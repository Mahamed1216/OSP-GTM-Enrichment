"""UI-side phase loop: enrich-all → score-all → content-all → deliver-all.

Reuses the per-lead atomic coroutines from src/. Per-lead errors are caught,
reported via the on_update callback, and do NOT abort subsequent leads or
phases. Different traversal from CLI (which does end-to-end per lead), but the
DB writes are identical.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from sqlalchemy import select

from app.lib.async_runner import run_async
from app.lib.db_queries import get_content_lead_ids, get_scored_lead_ids
from src.config import settings
from src.content.call_script import generate_call_script
from src.content.email import generate_email
from src.content.linkedin_msg import generate_linkedin_msg
from src.db import session_scope
from src.delivery.instantly import deliver_email
from src.enrichment.waterfall import enrich_lead
from src.models import GeneratedContent, Score
from src.prompts.email import (
    PROMPT_VERSION as EMAIL_PROMPT_VERSION,
    current_email_prompt_fingerprint,
)
from src.scoring import score_lead

# Per-lead hard cap. The Anthropic SDK has its own timeouts, but a wedged
# socket can still hang the loop past sensible bounds; 45s matches the
# resume-mode UX the operator expects (skip this lead, move on).
_BULK_REGEN_PER_LEAD_TIMEOUT_SEC = 45.0


@dataclass
class PhaseUpdate:
    phase: str  # "enrichment" | "scoring" | "content" | "delivery"
    lead_id: int
    idx: int
    total: int
    ok: bool
    error: str | None = None
    payload: dict[str, Any] | None = None


def _emit(on_update: Callable[[PhaseUpdate], None] | None, update: PhaseUpdate) -> None:
    if on_update is None:
        return
    try:
        on_update(update)
    except Exception:
        # A failing UI callback must not abort the pipeline.
        pass


# ---------- Per-phase async loops ----------

async def _phase_enrich(
    lead_ids: list[int],
    on_update: Callable[[PhaseUpdate], None] | None,
    *,
    force_refresh: bool = False,
) -> None:
    total = len(lead_ids)
    for idx, lid in enumerate(lead_ids, start=1):
        try:
            payload = await enrich_lead(lid)
            extra = {"refreshed": True} if force_refresh else {}
            merged: dict[str, Any] = {**(payload or {}), **extra} if isinstance(payload, dict) else extra
            _emit(on_update, PhaseUpdate("enrichment", lid, idx, total, True, None, merged or payload))
        except Exception as exc:
            _emit(on_update, PhaseUpdate("enrichment", lid, idx, total, False, str(exc)))


async def _phase_score(
    lead_ids: list[int],
    on_update: Callable[[PhaseUpdate], None] | None,
    *,
    force_refresh: bool = False,
) -> None:
    total = len(lead_ids)
    # Force mode bypasses the "already scored" guard so the Score row's
    # scored_at moves forward and any tier/rationale changes from the new
    # enrichment propagate.
    already_scored = set() if force_refresh else get_scored_lead_ids(lead_ids)
    for idx, lid in enumerate(lead_ids, start=1):
        if lid in already_scored:
            _emit(
                on_update,
                PhaseUpdate(
                    "scoring", lid, idx, total, True,
                    payload={"skipped": True, "reason": "already scored"},
                ),
            )
            continue
        try:
            result = await score_lead(lid)
            _emit(
                on_update,
                PhaseUpdate(
                    "scoring", lid, idx, total, True,
                    payload={
                        "score": result.score,
                        "tier": result.tier,
                        "refreshed": bool(force_refresh),
                    },
                ),
            )
        except Exception as exc:
            _emit(on_update, PhaseUpdate("scoring", lid, idx, total, False, str(exc)))


def _supersede_previous_active(lead_id: int, kind: str) -> bool:
    """After a fresh GeneratedContent row was inserted for (lead_id, kind),
    mark the prior active row as superseded by it.

    Picks the two most-recent non-superseded rows for the lead/kind and
    points the older one at the newer one. Returns True when a supersede
    pointer was actually written (i.e. an older active row existed).
    """
    with session_scope() as s:
        rows = s.execute(
            select(GeneratedContent.id)
            .where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == kind,
                GeneratedContent.superseded_by_id.is_(None),
            )
            .order_by(GeneratedContent.id.desc())
        ).all()
        ids = [int(r[0]) for r in rows]
        if len(ids) < 2:
            return False
        new_id, old_id = ids[0], ids[1]
        old_row = s.get(GeneratedContent, old_id)
        if old_row is not None:
            old_row.superseded_by_id = new_id
            return True
    return False


_KIND_GENERATOR = {
    "email": generate_email,
    "call_script": generate_call_script,
    "linkedin_msg": generate_linkedin_msg,
}


async def _phase_content(
    lead_ids: list[int],
    on_update: Callable[[PhaseUpdate], None] | None,
    *,
    run_email: bool = True,
    run_call_script: bool = True,
    run_linkedin_msg: bool = True,
    force_refresh: bool = False,
) -> None:
    """Per lead, generate the enabled content kinds in parallel.

    Per-kind idempotency: if a GeneratedContent row of a given kind already
    exists for the lead, that kind is skipped (no API call). A lead with email
    but no call_script re-attempts only call_script. Kinds disabled by their
    ``run_*`` flag are skipped without calling the generator.

    ``force_refresh=True`` bypasses the per-kind "already exists" guard for
    every enabled kind. A new GeneratedContent row is generated and the
    prior active row is wired with ``superseded_by_id`` so the version
    chain and ratings stay intact.
    """
    total = len(lead_ids)
    # In force mode, the "already exists" filter is a no-op — we want to
    # regenerate every enabled kind regardless.
    already_email = (
        get_content_lead_ids(lead_ids, "email")
        if run_email and not force_refresh else set()
    )
    already_call = (
        get_content_lead_ids(lead_ids, "call_script")
        if run_call_script and not force_refresh else set()
    )
    already_li = (
        get_content_lead_ids(lead_ids, "linkedin_msg")
        if run_linkedin_msg and not force_refresh else set()
    )
    for idx, lid in enumerate(lead_ids, start=1):
        # Track which kinds we'll actually generate so we can wire supersede
        # pointers and report per-kind status after the gather.
        kinds_to_run: list[str] = []
        skipped_kinds: list[str] = []
        if not run_email:
            skipped_kinds.append("email")
        elif lid in already_email:
            skipped_kinds.append("email")
        else:
            kinds_to_run.append("email")
        if not run_call_script:
            skipped_kinds.append("call_script")
        elif lid in already_call:
            skipped_kinds.append("call_script")
        else:
            kinds_to_run.append("call_script")
        if not run_linkedin_msg:
            skipped_kinds.append("linkedin_msg")
        elif lid in already_li:
            skipped_kinds.append("linkedin_msg")
        else:
            kinds_to_run.append("linkedin_msg")

        if not kinds_to_run:
            _emit(
                on_update,
                PhaseUpdate(
                    "content", lid, idx, total, True,
                    payload={"skipped": True, "reason": "all kinds already complete"},
                ),
            )
            continue

        tasks = [_KIND_GENERATOR[k](lid) for k in kinds_to_run]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            _emit(on_update, PhaseUpdate("content", lid, idx, total, False, str(exc)))
            continue

        regenerated_kinds: list[str] = []
        failed_kinds: list[str] = []
        errors: list[Exception] = []
        for kind, res in zip(kinds_to_run, results):
            if isinstance(res, Exception):
                failed_kinds.append(kind)
                errors.append(res)
                continue
            # In force mode, the generator just inserted a new active row
            # alongside the previous active row — wire the supersede pointer
            # so the version chain stays clean and the Lead Detail page
            # surfaces the fresh row as the active one.
            if force_refresh:
                try:
                    _supersede_previous_active(lid, kind)
                except Exception as exc:
                    # Failing to wire the pointer is not a content-gen
                    # failure — the new row IS in the DB. Log via payload
                    # so the UI shows it but the kind still counts as
                    # refreshed.
                    errors.append(exc)
            regenerated_kinds.append(kind)

        ok = not failed_kinds
        err_msg = (
            "; ".join(f"{type(e).__name__}: {e}" for e in errors) if errors else None
        )
        payload: dict[str, Any] = {}
        if regenerated_kinds:
            payload["regenerated_kinds"] = regenerated_kinds
        if skipped_kinds:
            payload["skipped_kinds"] = skipped_kinds
        if failed_kinds:
            payload["failed_kinds"] = failed_kinds
        if force_refresh:
            payload["forced"] = True
        _emit(on_update, PhaseUpdate("content", lid, idx, total, ok, err_msg, payload or None))


async def _phase_deliver(
    lead_ids: list[int],
    *,
    dry_run: bool,
    on_update: Callable[[PhaseUpdate], None] | None,
) -> None:
    total = len(lead_ids)
    for idx, lid in enumerate(lead_ids, start=1):
        try:
            result = await deliver_email(lid, dry_run=dry_run)
            payload = {
                "delivered": result.delivered,
                "skip_reason": result.skip_reason,
                "dry_run": result.dry_run,
            }
            _emit(on_update, PhaseUpdate("delivery", lid, idx, total, True, payload=payload))
        except Exception as exc:
            _emit(on_update, PhaseUpdate("delivery", lid, idx, total, False, str(exc)))


# ---------- Bulk regenerate (force, supersede-preserving) ----------

_KIND_REGEN_DISPATCH = {
    "email": generate_email,
    "call_script": generate_call_script,
    "linkedin_msg": generate_linkedin_msg,
}


async def _phase_bulk_regen(
    lead_ids: list[int],
    on_update: Callable[[PhaseUpdate], None] | None,
    *,
    skip_current_version: bool = True,
) -> None:
    """Regenerate the active **email** GeneratedContent row for each lead.

    Call scripts and LinkedIn DMs are intentionally left untouched — this
    entry point is the bulk EMAIL regenerate path driven by Section 3 on
    the Run Pipeline page.

    Resume semantics: when `skip_current_version` is True (the default),
    leads whose latest active email row already carries
    `prompt_version == EMAIL_PROMPT_VERSION` are skipped — so re-running
    after a partial completion picks up where it left off instead of
    rewriting work the previous run already finished.

    Per-lead failure isolation: each generator call is wrapped in
    `asyncio.wait_for(..., 45s)` plus a broad except. Timeouts, Anthropic
    rate limits, JSON parse errors, missing data — all surface as a
    `PhaseUpdate(ok=False)` and the loop continues to the next lead.
    Successful rows are committed inside `generate_email` immediately
    (one session per lead), so a crash mid-batch keeps prior work."""
    # Snapshot the current fingerprint once for the whole batch so a
    # mid-batch prompt edit doesn't shift the skip threshold under us.
    current_fingerprint = current_email_prompt_fingerprint()

    total = len(lead_ids)
    for idx, lid in enumerate(lead_ids, start=1):
        # Re-read state inside the loop so resume mode also picks up
        # rows that were updated by another session/tab since the
        # candidate filter was computed.
        with session_scope() as s:
            row = s.execute(
                select(
                    GeneratedContent.id,
                    GeneratedContent.prompt_fingerprint,
                )
                .where(
                    GeneratedContent.lead_id == lid,
                    GeneratedContent.kind == "email",
                    GeneratedContent.superseded_by_id.is_(None),
                )
                .order_by(GeneratedContent.id.desc())
            ).first()
        if row is None:
            old_id: int | None = None
            old_fingerprint: str | None = None
        else:
            old_id = int(row[0])
            old_fingerprint = row[1]

        # Resume mode = "skip if the latest row was produced by the SAME
        # prompt text that's live right now". A row with no fingerprint
        # (pre-feature data) is intentionally NOT current — those are the
        # bad-prompt rows we want resume to catch.
        if (
            skip_current_version
            and old_fingerprint is not None
            and old_fingerprint == current_fingerprint
        ):
            _emit(
                on_update,
                PhaseUpdate(
                    "content", lid, idx, total, True,
                    payload={
                        "skipped": True,
                        "skipped_current_version": True,
                        "reason": (
                            f"already at current prompt fingerprint "
                            f"({old_fingerprint})"
                        ),
                    },
                ),
            )
            continue

        # Run the generator with a per-lead timeout. The generator commits
        # the new GeneratedContent row in its own session_scope, so a
        # timeout/crash on this lead does not affect already-saved work.
        try:
            await asyncio.wait_for(
                generate_email(lid),
                timeout=_BULK_REGEN_PER_LEAD_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            _emit(
                on_update,
                PhaseUpdate(
                    "content", lid, idx, total, False,
                    error=f"timeout after {_BULK_REGEN_PER_LEAD_TIMEOUT_SEC:.0f}s",
                    payload={"timed_out": True},
                ),
            )
            continue
        except Exception as exc:
            _emit(
                on_update,
                PhaseUpdate(
                    "content", lid, idx, total, False,
                    error=f"email: {type(exc).__name__}: {exc}",
                ),
            )
            continue

        # If there was an old active email row, wire the supersede pointer
        # so the version chain + ratings stay intact. If there was none
        # (lead had no email at all), nothing to supersede — the generator
        # already inserted the fresh row.
        if old_id is not None:
            with session_scope() as s:
                new_row = s.execute(
                    select(GeneratedContent)
                    .where(
                        GeneratedContent.lead_id == lid,
                        GeneratedContent.kind == "email",
                        GeneratedContent.id != old_id,
                        GeneratedContent.superseded_by_id.is_(None),
                    )
                    .order_by(GeneratedContent.id.desc())
                ).scalars().first()
                if new_row is None:
                    _emit(
                        on_update,
                        PhaseUpdate(
                            "content", lid, idx, total, False,
                            error="email: generator produced no new row",
                        ),
                    )
                    continue
                source = s.get(GeneratedContent, old_id)
                if source is not None:
                    source.superseded_by_id = new_row.id

        _emit(on_update, PhaseUpdate("content", lid, idx, total, True))


def bulk_regenerate_content(
    lead_ids: list[int],
    *,
    on_update: Callable[[PhaseUpdate], None] | None = None,
    skip_current_version: bool = True,
) -> None:
    """Regenerate **email** content for the given leads. Call scripts and
    LinkedIn DMs are left untouched. Old email rows are preserved with
    `superseded_by_id` pointing at the new row.

    `skip_current_version=True` (default) is resume mode — leads whose
    latest active email is already at `EMAIL_PROMPT_VERSION` are skipped.
    Pass False to force-regenerate every lead in the list."""
    if not lead_ids:
        return
    run_async(
        _phase_bulk_regen(
            lead_ids, on_update, skip_current_version=skip_current_version
        )
    )


def get_email_regen_unfinished_lead_ids(lead_ids: list[int]) -> list[int]:
    """Subset of `lead_ids` whose latest active email was NOT generated
    under the current prompt fingerprint. Used by the Run Pipeline page
    to count "unfinished" leads when resume mode is on.

    A lead counts as unfinished if any of:
      - its newest non-superseded email row has a different
        `prompt_fingerprint` from the live one, OR
      - that row has `prompt_fingerprint IS NULL` (pre-feature data —
        intentionally NOT treated as current so resume catches it), OR
      - it has no active email row at all (regenerate will create one).
    """
    if not lead_ids:
        return []
    current_fp = current_email_prompt_fingerprint()
    with session_scope() as s:
        rows = s.execute(
            select(
                GeneratedContent.lead_id,
                GeneratedContent.prompt_fingerprint,
                GeneratedContent.id,
            )
            .where(
                GeneratedContent.lead_id.in_(lead_ids),
                GeneratedContent.kind == "email",
                GeneratedContent.superseded_by_id.is_(None),
            )
            .order_by(
                GeneratedContent.lead_id.asc(), GeneratedContent.id.desc()
            )
        ).all()
    latest_fp_by_lead: dict[int, str | None] = {}
    for lid_row, fp, _id in rows:
        if int(lid_row) not in latest_fp_by_lead:
            latest_fp_by_lead[int(lid_row)] = fp
    return [
        lid for lid in lead_ids
        if latest_fp_by_lead.get(int(lid)) is None
        or latest_fp_by_lead.get(int(lid)) != current_fp
    ]


# ---------- Filters between phases (read what's now in the DB) ----------

def _send_eligible_lead_ids(candidate_ids: Iterable[int]) -> list[int]:
    """Return the subset of candidate ids whose tier passes settings.send_min_tier."""
    candidate_ids = list(candidate_ids)
    if not candidate_ids:
        return []
    with session_scope() as s:
        rows = s.execute(
            select(Score.lead_id, Score.tier).where(Score.lead_id.in_(candidate_ids))
        ).all()
    return [lid for (lid, tier) in rows if tier and settings.should_send(tier)]


def _has_email_content_lead_ids(candidate_ids: Iterable[int]) -> list[int]:
    candidate_ids = list(candidate_ids)
    if not candidate_ids:
        return []
    with session_scope() as s:
        rows = s.execute(
            select(GeneratedContent.lead_id)
            .where(
                GeneratedContent.lead_id.in_(candidate_ids),
                GeneratedContent.kind == "email",
            )
            .distinct()
        ).all()
    return [r[0] for r in rows]


# ---------- Public entry ----------

def process_single_lead(
    lead_id: int,
    *,
    dry_run: bool,
    run_enrichment: bool = True,
    run_scoring: bool = True,
    run_email: bool = True,
    run_call_script: bool = True,
    run_linkedin_msg: bool = True,
    force_refresh: bool = False,
    on_update: Callable[[PhaseUpdate], None] | None = None,
) -> None:
    """Run enrich → score → content → deliver for a single lead, in order.

    Each phase is gated by its own flag. The three content kinds (email,
    call script, LinkedIn DM) are each individually gated. Eligibility
    (tier threshold) is checked after scoring within the same call. Delivery
    is gated on ``run_email`` — only the email kind feeds the Instantly
    deliver step, so if email isn't generated there's nothing to deliver.

    Per-phase errors are surfaced via ``on_update`` (when provided) by the
    underlying ``_phase_*`` coroutines. This function itself does not raise
    — caller can rely on it returning even if a phase failed for this lead.
    """
    if run_enrichment:
        run_async(_phase_enrich([lead_id], on_update, force_refresh=force_refresh))

    if run_scoring:
        run_async(_phase_score([lead_id], on_update, force_refresh=force_refresh))

    if run_email or run_call_script or run_linkedin_msg:
        eligible = _send_eligible_lead_ids([lead_id])
        if eligible:
            run_async(_phase_content(
                eligible,
                on_update,
                run_email=run_email,
                run_call_script=run_call_script,
                run_linkedin_msg=run_linkedin_msg,
                force_refresh=force_refresh,
            ))
            if run_email:
                have_email = _has_email_content_lead_ids(eligible)
                if have_email:
                    run_async(_phase_deliver(have_email, dry_run=dry_run, on_update=on_update))


def run_phased_pipeline(
    lead_ids: list[int],
    *,
    dry_run: bool,
    on_update: Callable[[PhaseUpdate], None] | None = None,
    run_enrichment: bool = True,
    run_scoring: bool = True,
    run_email: bool = True,
    run_call_script: bool = True,
    run_linkedin_msg: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Run the full pipeline per-lead and return a summary dict.

    Each iteration: enrich → score → content+deliver for one lead before
    moving to the next. This matters because the previous batch-shaped
    traversal (enrich-all, then score-all, then content-all) ran content
    gen as a second pass that re-read the score table — fine in isolation,
    but the per-lead version keeps related work adjacent in the UI feed
    and means a single lead reaches its final state before the next
    starts.

    Step flags gate individual steps. The three content kinds are each
    gated independently. Delivery is gated on ``run_email`` — only email
    feeds the deliver step.
    """
    if not lead_ids:
        return {"enriched": 0, "scored": 0, "content": 0, "delivered": 0, "skipped": 0}

    for lid in lead_ids:
        process_single_lead(
            lid,
            dry_run=dry_run,
            run_enrichment=run_enrichment,
            run_scoring=run_scoring,
            run_email=run_email,
            run_call_script=run_call_script,
            run_linkedin_msg=run_linkedin_msg,
            force_refresh=force_refresh,
            on_update=on_update,
        )

    return _summary(lead_ids)


def _summary(lead_ids: list[int]) -> dict[str, Any]:
    """Read-only DB scan to summarize what landed for the run's lead set."""
    from sqlalchemy import func

    from src.models import Engagement, Enrichment, Lead

    summary: dict[str, Any] = {
        "lead_ids": lead_ids,
        "enriched": 0,
        "scored": 0,
        "content": 0,
        "delivered": 0,
        "tiers": {"A": 0, "B": 0, "C": 0},
    }
    with session_scope() as s:
        summary["enriched"] = (
            s.execute(
                select(func.count(Enrichment.id)).where(Enrichment.lead_id.in_(lead_ids))
            ).scalar() or 0
        )
        summary["scored"] = (
            s.execute(
                select(func.count(Score.id)).where(Score.lead_id.in_(lead_ids))
            ).scalar() or 0
        )
        summary["content"] = (
            s.execute(
                select(func.count(func.distinct(GeneratedContent.lead_id))).where(
                    GeneratedContent.lead_id.in_(lead_ids),
                    GeneratedContent.kind == "email",
                )
            ).scalar() or 0
        )
        summary["delivered"] = (
            s.execute(
                select(func.count(GeneratedContent.id)).where(
                    GeneratedContent.lead_id.in_(lead_ids),
                    GeneratedContent.kind == "email",
                    GeneratedContent.delivered_at.is_not(None),
                )
            ).scalar() or 0
        )
        tier_rows = s.execute(
            select(Score.tier, func.count(Score.id))
            .where(Score.lead_id.in_(lead_ids))
            .group_by(Score.tier)
        ).all()
        for tier, count in tier_rows:
            if tier in summary["tiers"]:
                summary["tiers"][tier] = int(count)
    return summary
