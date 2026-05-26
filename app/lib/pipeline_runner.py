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
from src.scoring import score_lead


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
) -> None:
    total = len(lead_ids)
    for idx, lid in enumerate(lead_ids, start=1):
        try:
            payload = await enrich_lead(lid)
            _emit(on_update, PhaseUpdate("enrichment", lid, idx, total, True, None, payload))
        except Exception as exc:
            _emit(on_update, PhaseUpdate("enrichment", lid, idx, total, False, str(exc)))


async def _phase_score(
    lead_ids: list[int],
    on_update: Callable[[PhaseUpdate], None] | None,
) -> None:
    total = len(lead_ids)
    already_scored = get_scored_lead_ids(lead_ids)
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
                    payload={"score": result.score, "tier": result.tier},
                ),
            )
        except Exception as exc:
            _emit(on_update, PhaseUpdate("scoring", lid, idx, total, False, str(exc)))


async def _phase_content(
    lead_ids: list[int],
    on_update: Callable[[PhaseUpdate], None] | None,
    *,
    run_email: bool = True,
    run_call_script: bool = True,
    run_linkedin_msg: bool = True,
) -> None:
    """Per lead, generate the enabled content kinds in parallel.

    Per-kind idempotency: if a GeneratedContent row of a given kind already
    exists for the lead, that kind is skipped (no API call). A lead with email
    but no call_script re-attempts only call_script. Kinds disabled by their
    ``run_*`` flag are skipped without calling the generator.
    """
    total = len(lead_ids)
    already_email = get_content_lead_ids(lead_ids, "email") if run_email else set()
    already_call = get_content_lead_ids(lead_ids, "call_script") if run_call_script else set()
    already_li = get_content_lead_ids(lead_ids, "linkedin_msg") if run_linkedin_msg else set()
    for idx, lid in enumerate(lead_ids, start=1):
        tasks: list = []
        skipped_kinds: list[str] = []
        if not run_email:
            skipped_kinds.append("email")
        elif lid in already_email:
            skipped_kinds.append("email")
        else:
            tasks.append(generate_email(lid))
        if not run_call_script:
            skipped_kinds.append("call_script")
        elif lid in already_call:
            skipped_kinds.append("call_script")
        else:
            tasks.append(generate_call_script(lid))
        if not run_linkedin_msg:
            skipped_kinds.append("linkedin_msg")
        elif lid in already_li:
            skipped_kinds.append("linkedin_msg")
        else:
            tasks.append(generate_linkedin_msg(lid))

        if not tasks:
            _emit(
                on_update,
                PhaseUpdate(
                    "content", lid, idx, total, True,
                    payload={"skipped": True, "reason": "all kinds already complete"},
                ),
            )
            continue

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            ok = not errors
            err_msg = "; ".join(f"{type(e).__name__}: {e}" for e in errors) if errors else None
            payload = {"skipped_kinds": skipped_kinds} if skipped_kinds else None
            _emit(on_update, PhaseUpdate("content", lid, idx, total, ok, err_msg, payload))
        except Exception as exc:
            _emit(on_update, PhaseUpdate("content", lid, idx, total, False, str(exc)))


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
) -> None:
    """For each lead, regenerate every active (non-superseded) GeneratedContent
    row. Each generator call inserts a new row, after which we wire
    `old.superseded_by_id = new.id` so the version chain is preserved."""
    total = len(lead_ids)
    for idx, lid in enumerate(lead_ids, start=1):
        with session_scope() as s:
            rows = s.execute(
                select(GeneratedContent.id, GeneratedContent.kind)
                .where(
                    GeneratedContent.lead_id == lid,
                    GeneratedContent.superseded_by_id.is_(None),
                )
                .order_by(GeneratedContent.id.asc())
            ).all()
        active = [(int(r[0]), str(r[1])) for r in rows]
        if not active:
            _emit(
                on_update,
                PhaseUpdate(
                    "content", lid, idx, total, True,
                    payload={"skipped": True, "reason": "no active content"},
                ),
            )
            continue

        errors: list[str] = []
        for old_id, kind in active:
            generator = _KIND_REGEN_DISPATCH.get(kind)
            if generator is None:
                errors.append(f"unknown kind: {kind}")
                continue
            try:
                await generator(lid)
            except Exception as exc:
                errors.append(f"{kind}: {type(exc).__name__}: {exc}")
                continue
            with session_scope() as s:
                new_row = s.execute(
                    select(GeneratedContent)
                    .where(
                        GeneratedContent.lead_id == lid,
                        GeneratedContent.kind == kind,
                        GeneratedContent.id != old_id,
                        GeneratedContent.superseded_by_id.is_(None),
                    )
                    .order_by(GeneratedContent.id.desc())
                ).scalars().first()
                if new_row is None:
                    errors.append(f"{kind}: generator produced no new row")
                    continue
                source = s.get(GeneratedContent, old_id)
                if source is not None:
                    source.superseded_by_id = new_row.id

        ok = not errors
        err_msg = "; ".join(errors) if errors else None
        _emit(on_update, PhaseUpdate("content", lid, idx, total, ok, err_msg))


def bulk_regenerate_content(
    lead_ids: list[int],
    *,
    on_update: Callable[[PhaseUpdate], None] | None = None,
) -> None:
    """Force-regenerate content for the given leads. Old rows are preserved
    with `superseded_by_id` pointing at the new row. Skips idempotency by
    design — this is the user-invoked refresh path."""
    if not lead_ids:
        return
    run_async(_phase_bulk_regen(lead_ids, on_update))


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
        run_async(_phase_enrich([lead_id], on_update))

    if run_scoring:
        run_async(_phase_score([lead_id], on_update))

    if run_email or run_call_script or run_linkedin_msg:
        eligible = _send_eligible_lead_ids([lead_id])
        if eligible:
            run_async(_phase_content(
                eligible,
                on_update,
                run_email=run_email,
                run_call_script=run_call_script,
                run_linkedin_msg=run_linkedin_msg,
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
