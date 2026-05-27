"""Approval-gated self-improvement loop.

Design contract (the previous iteration of this module violated several
of these — the rewrite enforces them):

  1. Single source of truth for metrics. `kpi_view(snapshot)` returns the
     same integers + rates the Engagement page's KPI cards render. The
     loop ALWAYS reads from `kpi_view()`; it never re-computes from raw
     fields. KPI cards and the loop therefore can't disagree on bounce
     rate, open rate, sample size, etc.

  2. Bounce and delivery are NEVER prompt changes. They're diagnostic-
     only states. The diagnose() function refuses to attach a
     `proposed_addendum` to them; the persistence layer refuses to save
     them as a `PromptRecommendation` (those rows are for prompt-edit
     candidates only).

  3. Sample-size and timing gates are PRE-CONDITIONS for proposing a
     prompt change, not labels on a forced recommendation:
       - sent < 50  → loop_status = "wait" (no recommendation, no row)
       - sent 50-199 → confidence = "low" → loop_status = "draft"
       - sent ≥ 200  → confidence = "standard" → loop_status =
         "ready_for_approval" (Approve button live)
       - latest send < 24h  AND bottleneck = reply_rate → "wait"
       - latest send < 48h  AND bottleneck = reply_rate → "diagnose_only"

  4. Reply-rate fix requires a HEALTHY open rate. If open rate is below
     target, the open-rate fix is the recommendation — never both.

  5. Proposed changes are SMALL ADDENDUMS, not full prompt rewrites. On
     approval, the addendum is APPENDED to the current overlay; rollback
     restores the previous overlay snapshot.

  6. Nothing here pushes to Instantly, regenerates already-sent emails,
     activates campaigns, or touches existing GeneratedContent rows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import case, func, select

from src.db import session_scope
from src.models import (
    Engagement,
    GeneratedContent,
    InstantlyAnalyticsSnapshot,
    PromptRecommendation,
)
from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY
from src.prompts.loader import get_effective_prompt, save_overlay

log = logging.getLogger(__name__)

# Default benchmarks. UI exposes these to the operator.
DEFAULT_OPEN_RATE_TARGET = 0.30
DEFAULT_REPLY_RATE_TARGET = 0.03
DEFAULT_BOUNCE_RATE_MAX = 0.03

# Sample-size and timing thresholds. Constants so tests can monkey-patch.
SAMPLE_WAIT_MAX = 50          # below this → "wait" (no recommendation)
SAMPLE_LOW_CONF_MAX = 200     # below this but ≥ wait → "draft" (low conf)
REPLY_WAIT_HOURS = 24         # below this, no reply-rate diagnosis at all
REPLY_DIAGNOSE_HOURS = 48     # below this, diagnose-only on reply-rate

# Loop status vocabulary — see module docstring.
LOOP_WAIT = "wait"
LOOP_DIAGNOSE = "diagnose_only"
LOOP_DRAFT = "draft"
LOOP_READY = "ready_for_approval"


# ---------------------------------------------------------------------------
# KPI view — single source of truth shared with the page's KPI cards.
# ---------------------------------------------------------------------------

def kpi_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Return the metrics dict the KPI cards AND the loop both read.

    Keys: contacted, sent, opens, replies, bounces (all ints) plus
    open_rate, reply_rate, bounce_rate (floats in [0, 1]). When the
    snapshot is None or empty, every value is zero.

    The Engagement page imports this and renders its KPI cards from the
    returned dict directly, so by construction the loop and the cards
    cannot disagree on any number.
    """
    if not snapshot:
        return {
            "contacted": 0, "sent": 0, "opens": 0, "replies": 0, "bounces": 0,
            "open_rate": 0.0, "reply_rate": 0.0, "bounce_rate": 0.0,
        }
    sent = int(snapshot.get("emails_sent_count") or 0)
    contacted = int(snapshot.get("contacted_count") or 0)
    opens = int(snapshot.get("open_count") or 0)
    replies = int(snapshot.get("reply_count") or 0)
    bounces = int(snapshot.get("bounced_count") or 0)
    denom = sent if sent > 0 else 1
    return {
        "contacted": contacted,
        "sent": sent,
        "opens": opens,
        "replies": replies,
        "bounces": bounces,
        "open_rate": opens / denom,
        "reply_rate": replies / denom,
        "bounce_rate": bounces / denom,
    }


# ---------------------------------------------------------------------------
# Diagnosis dataclass — what the loop returns to the UI.
# ---------------------------------------------------------------------------

@dataclass
class Diagnosis:
    """One pass of the loop. The UI renders this; persistence only saves
    it when `loop_status in {draft, ready_for_approval}` and only those
    states expose Approve buttons."""

    loop_status: str                     # wait | diagnose_only | draft | ready_for_approval
    bottleneck: str                      # open_rate | reply_rate | bounce_rate | delivery | none
    channel: Optional[str]               # email | None (None for non-prompt actions)
    confidence: str                      # insufficient | low | standard
    diagnosis: str                       # human prose, names which metric triggered
    current_metric_label: str
    current_metric_value: float
    target_metric_value: float
    recommended_change: str              # operator action — prompt edit OR ops action
    expected_impact: str
    risk_level: str                      # low | medium | high
    proposed_addendum: Optional[str]     # SHORT addendum text, appended on approval
    previous_prompt_snapshot: Optional[str]
    sample_size: int                     # sent emails (the denominator)
    hours_since_latest_send: Optional[float]

    @property
    def is_actionable_prompt_change(self) -> bool:
        """True iff this diagnosis would, on approval, write to the
        prompt overlay. Bounce + delivery are always False."""
        return (
            self.proposed_addendum is not None
            and self.channel is not None
            and self.loop_status in (LOOP_DRAFT, LOOP_READY)
        )


# ---------------------------------------------------------------------------
# Addendum templates — short and specific. Each one names the triggering
# metric explicitly so the operator can see WHY in the diff preview.
# ---------------------------------------------------------------------------

def _open_rate_addendum(current_rate: float, target_rate: float) -> str:
    return (
        "# OPEN-RATE FIX — added by self-improvement loop\n"
        f"# Triggered by: open rate {current_rate * 100:.1f}% "
        f"(target {target_rate * 100:.0f}%).\n"
        "# Scope: subject lines only. Body copy unchanged.\n"
        "- Subjects under 4 words. Prefer 2 or 3.\n"
        "- Reference one concrete entity (company, product, role) from enrichment.\n"
        "- No generic angles (\"quick question\", \"intro\", \"checking in\").\n"
        "- Lowercase. No emoji, no punctuation in the subject line.\n"
    )


def _reply_rate_addendum(current_rate: float, target_rate: float) -> str:
    return (
        "# REPLY-RATE FIX — added by self-improvement loop\n"
        f"# Triggered by: reply rate {current_rate * 100:.2f}% "
        f"(target {target_rate * 100:.0f}%) with a HEALTHY open rate.\n"
        "# Scope: body length, CTA, hedging. Subject lines unchanged.\n"
        "- Body 50–80 words. Cut every sentence without a named entity.\n"
        "- CTA must invite a one-word reply. No call/meeting CTAs.\n"
        "- Lead with a concrete value comparison or a number.\n"
        "- Remove hedging (\"might be a fit\", \"if you're open\", \"no worries if not\").\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _latest_send_timestamp() -> datetime | None:
    """MAX(delivered_at) over email GeneratedContent rows actually sent.

    Used to gate the reply-rate diagnosis on the 24h/48h timing rules.
    Reads from the local DB rather than Instantly because Instantly's
    analytics snapshot doesn't expose per-campaign first/last send
    timestamps in a stable shape.
    """
    with session_scope() as session:
        ts = session.execute(
            select(func.max(GeneratedContent.delivered_at)).where(
                GeneratedContent.kind == "email",
                GeneratedContent.delivery_status == "sent",
            )
        ).scalar()
    return ts


def _confidence_band(sample_size: int) -> str:
    if sample_size < SAMPLE_WAIT_MAX:
        return "insufficient"
    if sample_size < SAMPLE_LOW_CONF_MAX:
        return "low"
    return "standard"


def _loop_status_for_prompt_change(confidence: str) -> str:
    """Map a prompt-change candidate's confidence to a loop status."""
    if confidence == "insufficient":
        return LOOP_WAIT
    if confidence == "low":
        return LOOP_DRAFT
    return LOOP_READY


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

def diagnose(
    metrics: dict[str, Any],
    *,
    open_rate_target: float = DEFAULT_OPEN_RATE_TARGET,
    reply_rate_target: float = DEFAULT_REPLY_RATE_TARGET,
    bounce_rate_max: float = DEFAULT_BOUNCE_RATE_MAX,
    local_sent_count: int | None = None,
    now: datetime | None = None,
    latest_send: datetime | None = None,
) -> Diagnosis:
    """Single entrypoint. `metrics` MUST be the dict returned by
    `kpi_view()` so KPI cards and this function agree by construction.

    Priority (matches the spec's diagnosis rules verbatim):
      1. Bounce rate above max → diagnose-only, list-quality action.
      2. Open rate below target → subject-line addendum candidate.
         Gates: sample size only (opens happen fast).
      3. Reply rate below target AND open rate healthy → body/CTA
         addendum candidate. Gates: sample size + 48-hour timing.
      4. Else → "none" / green.

    Delivery mismatch is intentionally NOT a loop diagnosis. The page
    surfaces sync hygiene as a top-of-page warning where it belongs;
    the loop focuses on copy-affecting diagnoses so a delivery gap
    doesn't crowd out an open-rate fix that the operator could act on.
    `local_sent_count` is still accepted for backward-compat but is no
    longer consulted here.
    """
    sent = int(metrics["sent"])
    contacted = int(metrics["contacted"])
    bounce_rate = float(metrics["bounce_rate"])
    open_rate = float(metrics["open_rate"])
    reply_rate = float(metrics["reply_rate"])
    sample_size = sent
    confidence = _confidence_band(sample_size)

    if latest_send is None:
        latest_send = _latest_send_timestamp()
    if now is None:
        now = datetime.utcnow()
    hours_since_send: float | None = None
    if latest_send is not None:
        hours_since_send = max(0.0, (now - latest_send).total_seconds() / 3600.0)

    # ---- 1) Bounce rate — always diagnose-only ----------------------------
    if sent > 0 and bounce_rate > bounce_rate_max:
        return Diagnosis(
            loop_status=LOOP_DIAGNOSE,
            bottleneck="bounce_rate",
            channel=None,
            confidence=confidence,
            diagnosis=(
                f"Bounce rate is {bounce_rate * 100:.1f}% "
                f"(target ≤ {bounce_rate_max * 100:.1f}%). "
                "List quality / verification is the issue, not the copy."
            ),
            current_metric_label="Bounce rate",
            current_metric_value=bounce_rate,
            target_metric_value=bounce_rate_max,
            recommended_change=(
                "Re-verify unverified leads before the next push (Settings → "
                "Verify cache). Strip catch-all and disposable domains. "
                "Do NOT change the prompt — bounces are not a copy problem."
            ),
            expected_impact="Bounce rate drops below target within one send cycle.",
            risk_level="low",
            proposed_addendum=None,
            previous_prompt_snapshot=None,
            sample_size=sample_size,
            hours_since_latest_send=hours_since_send,
        )

    # Delivery mismatch is handled by the page-level sync-hygiene warning,
    # not by this loop — see module docstring + spec rule #3. The
    # `local_sent_count` arg stays in the signature so existing callers
    # don't break.

    # ---- Sample gate (applies only to prompt-change candidates) ----------
    insufficient = confidence == "insufficient"

    # ---- 3) Open rate — subject lines ------------------------------------
    if sent > 0 and open_rate < open_rate_target:
        if insufficient:
            return Diagnosis(
                loop_status=LOOP_WAIT,
                bottleneck="open_rate",
                channel="email",
                confidence=confidence,
                diagnosis=(
                    f"Open rate is {open_rate * 100:.1f}% (target "
                    f"{open_rate_target * 100:.0f}%) on only {sample_size} "
                    f"sent. Not enough data for a strong recommendation yet."
                ),
                current_metric_label="Open rate",
                current_metric_value=open_rate,
                target_metric_value=open_rate_target,
                recommended_change=(
                    f"Wait for at least {SAMPLE_WAIT_MAX} sends before drafting "
                    "a subject-line addendum."
                ),
                expected_impact="—",
                risk_level="low",
                proposed_addendum=None,
                previous_prompt_snapshot=None,
                sample_size=sample_size,
                hours_since_latest_send=hours_since_send,
            )
        current = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY)
        return Diagnosis(
            loop_status=_loop_status_for_prompt_change(confidence),
            bottleneck="open_rate",
            channel="email",
            confidence=confidence,
            diagnosis=(
                f"Open rate is {open_rate * 100:.1f}% on {sample_size} sent "
                f"(target {open_rate_target * 100:.0f}%). Subject lines or "
                "targeting angle are weak — body copy is not in scope."
            ),
            current_metric_label="Open rate",
            current_metric_value=open_rate,
            target_metric_value=open_rate_target,
            recommended_change=(
                "Append a SUBJECT-LINE addendum to the email prompt: ≤4 "
                "words, one concrete entity, lowercase, no generic angles. "
                "Affects FUTURE generated emails only."
            ),
            expected_impact="Open rate +5 to +10 percentage points on subsequent sends.",
            risk_level="medium",
            proposed_addendum=_open_rate_addendum(open_rate, open_rate_target),
            previous_prompt_snapshot=current,
            sample_size=sample_size,
            hours_since_latest_send=hours_since_send,
        )

    # ---- 4) Reply rate — body / CTA. Gates: open healthy, timing, sample.
    open_is_healthy = open_rate >= open_rate_target
    if sent > 0 and reply_rate < reply_rate_target and open_is_healthy:
        # Timing gates take priority over sample gates because waiting longer
        # is cheap and the spec asks for these specific cutoffs.
        if hours_since_send is not None and hours_since_send < REPLY_WAIT_HOURS:
            return Diagnosis(
                loop_status=LOOP_WAIT,
                bottleneck="reply_rate",
                channel="email",
                confidence=confidence,
                diagnosis=(
                    f"Open rate is healthy ({open_rate * 100:.1f}%) but the "
                    f"latest send was {hours_since_send:.1f}h ago. Replies "
                    "typically arrive 24-72h after the first touch. Wait "
                    "before changing the body."
                ),
                current_metric_label="Reply rate",
                current_metric_value=reply_rate,
                target_metric_value=reply_rate_target,
                recommended_change=(
                    f"Wait at least {REPLY_WAIT_HOURS}h after the latest send "
                    "before drafting a body-copy change."
                ),
                expected_impact="—",
                risk_level="low",
                proposed_addendum=None,
                previous_prompt_snapshot=None,
                sample_size=sample_size,
                hours_since_latest_send=hours_since_send,
            )
        if hours_since_send is not None and hours_since_send < REPLY_DIAGNOSE_HOURS:
            return Diagnosis(
                loop_status=LOOP_DIAGNOSE,
                bottleneck="reply_rate",
                channel="email",
                confidence=confidence,
                diagnosis=(
                    f"Open rate is healthy ({open_rate * 100:.1f}%); reply rate "
                    f"is {reply_rate * 100:.2f}% but the latest send was only "
                    f"{hours_since_send:.1f}h ago. Diagnose only — do not "
                    "change the body until at least "
                    f"{REPLY_DIAGNOSE_HOURS}h have passed."
                ),
                current_metric_label="Reply rate",
                current_metric_value=reply_rate,
                target_metric_value=reply_rate_target,
                recommended_change=(
                    "Hold on body-copy edits. Re-evaluate after "
                    f"{REPLY_DIAGNOSE_HOURS}h. If the rate is still low and "
                    "open rate stays healthy, the addendum candidate will "
                    "appear here."
                ),
                expected_impact="—",
                risk_level="low",
                proposed_addendum=None,
                previous_prompt_snapshot=None,
                sample_size=sample_size,
                hours_since_latest_send=hours_since_send,
            )
        if insufficient:
            return Diagnosis(
                loop_status=LOOP_WAIT,
                bottleneck="reply_rate",
                channel="email",
                confidence=confidence,
                diagnosis=(
                    f"Reply rate {reply_rate * 100:.2f}% on only {sample_size} "
                    "sent. Not enough data for a strong recommendation yet."
                ),
                current_metric_label="Reply rate",
                current_metric_value=reply_rate,
                target_metric_value=reply_rate_target,
                recommended_change=(
                    f"Wait for at least {SAMPLE_WAIT_MAX} sends and "
                    f"{REPLY_DIAGNOSE_HOURS}h elapsed before drafting a "
                    "body-copy addendum."
                ),
                expected_impact="—",
                risk_level="low",
                proposed_addendum=None,
                previous_prompt_snapshot=None,
                sample_size=sample_size,
                hours_since_latest_send=hours_since_send,
            )
        current = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY)
        return Diagnosis(
            loop_status=_loop_status_for_prompt_change(confidence),
            bottleneck="reply_rate",
            channel="email",
            confidence=confidence,
            diagnosis=(
                f"Open rate is healthy ({open_rate * 100:.1f}%) but reply "
                f"rate is {reply_rate * 100:.2f}% on {sample_size} sent "
                f"(target {reply_rate_target * 100:.0f}%). Body copy / CTA "
                "is the bottleneck — subject lines are working."
            ),
            current_metric_label="Reply rate",
            current_metric_value=reply_rate,
            target_metric_value=reply_rate_target,
            recommended_change=(
                "Append a BODY/CTA addendum to the email prompt: tighten "
                "length to 50–80 words, force one-word CTA, drop hedging. "
                "Affects FUTURE generated emails only."
            ),
            expected_impact="Reply rate +1 to +2 percentage points on subsequent sends.",
            risk_level="medium",
            proposed_addendum=_reply_rate_addendum(reply_rate, reply_rate_target),
            previous_prompt_snapshot=current,
            sample_size=sample_size,
            hours_since_latest_send=hours_since_send,
        )

    # ---- 5) Green ---------------------------------------------------------
    return Diagnosis(
        loop_status=LOOP_DIAGNOSE,
        bottleneck="none",
        channel=None,
        confidence=confidence,
        diagnosis="All metrics meet or exceed the configured targets. No change recommended.",
        current_metric_label="Reply rate",
        current_metric_value=reply_rate,
        target_metric_value=reply_rate_target,
        recommended_change="No change recommended.",
        expected_impact="—",
        risk_level="low",
        proposed_addendum=None,
        previous_prompt_snapshot=None,
        sample_size=sample_size,
        hours_since_latest_send=hours_since_send,
    )


# ---------------------------------------------------------------------------
# Persistence — only for prompt-change candidates.
# ---------------------------------------------------------------------------

def save_recommendation(diag: Diagnosis) -> int | None:
    """Persist a Diagnosis IFF it's an actionable prompt-change candidate.

    Returns the new row id, or None when the diagnosis is wait /
    diagnose-only / none (those don't belong in the recommendation
    history because they have no addendum to approve or roll back).
    The previous module saved one for every render — that surfaced
    bounce/delivery noise in the history.
    """
    if not diag.is_actionable_prompt_change:
        return None
    # The DB column is named `proposed_prompt`; we persist the addendum
    # text under that column so existing migrations still apply. The
    # approval step appends it to the current overlay (not overwrites).
    initial_status = "ready_for_approval" if diag.loop_status == LOOP_READY else "draft"
    with session_scope() as session:
        rec = PromptRecommendation(
            bottleneck=diag.bottleneck,
            channel=diag.channel,
            diagnosis=diag.diagnosis,
            current_metric_label=diag.current_metric_label,
            current_metric_value=float(diag.current_metric_value),
            target_metric_value=float(diag.target_metric_value),
            recommended_change=diag.recommended_change,
            expected_impact=diag.expected_impact,
            risk_level=diag.risk_level,
            proposed_prompt=diag.proposed_addendum,
            previous_prompt_snapshot=diag.previous_prompt_snapshot,
            sample_size=int(diag.sample_size),
            low_confidence=(diag.confidence == "low"),
            status=initial_status,
        )
        session.add(rec)
        session.flush()
        return int(rec.id)


def approve_recommendation(rec_id: int, *, approved_by: str) -> dict[str, Any]:
    """Approve and APPEND the addendum to the current overlay.

    The addendum is appended, not substituted, so the operator's existing
    prompt edits are preserved. The PREVIOUS full overlay (pre-append)
    is what gets stashed for rollback, so undo always restores to the
    exact state before this approval.
    """
    with session_scope() as session:
        rec = session.get(PromptRecommendation, rec_id)
        if rec is None:
            raise ValueError(f"Recommendation {rec_id} not found")
        if rec.status == "approved":
            return {"id": rec_id, "status": "approved", "already": True}
        if not rec.channel or not rec.proposed_prompt:
            raise ValueError(
                "Recommendation has no addendum to apply — bounce/delivery "
                "diagnoses cannot be approved as prompt changes."
            )
        rec.status = "approved"
        rec.approved_by = approved_by
        rec.approved_at = datetime.utcnow()
        channel = rec.channel
        addendum = rec.proposed_prompt
        # Re-read the current overlay HERE (not at diagnosis time) so a
        # concurrent edit by the operator can't be silently overwritten.
        previous = get_effective_prompt(channel, DEFAULT_EMAIL_PROMPT_BODY)
        rec.previous_prompt_snapshot = previous

    appended = previous.rstrip() + "\n\n" + addendum.rstrip() + "\n"
    save_overlay(channel, appended)
    log.info(
        "self_improvement_addendum_applied",
        extra={"rec_id": rec_id, "channel": channel, "approved_by": approved_by},
    )
    return {"id": rec_id, "status": "approved", "channel": channel}


def reject_recommendation(rec_id: int, *, rejected_by: str) -> None:
    with session_scope() as session:
        rec = session.get(PromptRecommendation, rec_id)
        if rec is None:
            raise ValueError(f"Recommendation {rec_id} not found")
        rec.status = "rejected"
        rec.approved_by = rejected_by
        rec.approved_at = datetime.utcnow()


def save_as_draft(rec_id: int) -> None:
    with session_scope() as session:
        rec = session.get(PromptRecommendation, rec_id)
        if rec is None:
            raise ValueError(f"Recommendation {rec_id} not found")
        rec.status = "draft"


def rollback_recommendation(rec_id: int) -> dict[str, Any]:
    """Restore `previous_prompt_snapshot` for an approved row."""
    with session_scope() as session:
        rec = session.get(PromptRecommendation, rec_id)
        if rec is None:
            raise ValueError(f"Recommendation {rec_id} not found")
        if not rec.channel or rec.previous_prompt_snapshot is None:
            raise ValueError("Nothing to roll back — no channel/snapshot stored.")
        channel = rec.channel
        previous = rec.previous_prompt_snapshot
        rec.status = "rejected"
    save_overlay(channel, previous)
    log.info("self_improvement_rolled_back", extra={"rec_id": rec_id, "channel": channel})
    return {"id": rec_id, "channel": channel}


# ---------------------------------------------------------------------------
# Experiment tracking — per-prompt-version rollup
# ---------------------------------------------------------------------------

def performance_by_prompt_version() -> list[dict[str, Any]]:
    """Per-prompt-version rollup. Rows with sent < SAMPLE_WAIT_MAX are
    labeled low-confidence; the loop never proposes a prompt change from
    those rows (it doesn't read this table — it reads kpi_view), so the
    label here is purely informational for the operator."""
    stmt = (
        select(
            GeneratedContent.prompt_version.label("prompt_version"),
            GeneratedContent.prompt_fingerprint.label("prompt_fingerprint"),
            func.count(Engagement.id).label("sent"),
            func.sum(case((Engagement.opened.is_(True), 1), else_=0)).label("opened"),
            func.sum(case((Engagement.replied.is_(True), 1), else_=0)).label("replied"),
            func.sum(case((Engagement.bounced.is_(True), 1), else_=0)).label("bounced"),
        )
        .select_from(GeneratedContent)
        .join(Engagement, Engagement.content_id == GeneratedContent.id)
        .where(
            GeneratedContent.kind == "email",
            Engagement.sent.is_(True),
        )
        .group_by(
            GeneratedContent.prompt_version,
            GeneratedContent.prompt_fingerprint,
        )
        .order_by(func.count(Engagement.id).desc())
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        sent = int(r.sent or 0)
        opened = int(r.opened or 0)
        replied = int(r.replied or 0)
        bounced = int(r.bounced or 0)
        denom = sent if sent > 0 else 1
        out.append({
            "prompt_version": r.prompt_version or "—",
            "prompt_fingerprint": (r.prompt_fingerprint or "")[:8] or "—",
            "sent": sent,
            "opened": opened,
            "replied": replied,
            "bounced": bounced,
            "open_rate": opened / denom,
            "reply_rate": replied / denom,
            "bounce_rate": bounced / denom,
            "low_confidence": sent < SAMPLE_WAIT_MAX,
        })
    return out


def list_recommendations(limit: int = 20) -> list[dict[str, Any]]:
    """Most-recent PromptRecommendation rows for the audit log. Only
    actionable rows live here (wait/diagnose-only never write to this
    table), so the history stays signal-rich."""
    with session_scope() as session:
        rows = session.execute(
            select(PromptRecommendation)
            .order_by(PromptRecommendation.created_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "bottleneck": r.bottleneck,
                "channel": r.channel,
                "diagnosis": r.diagnosis,
                "current_metric_label": r.current_metric_label,
                "current_metric_value": float(r.current_metric_value or 0.0),
                "target_metric_value": float(r.target_metric_value or 0.0),
                "recommended_change": r.recommended_change,
                "expected_impact": r.expected_impact,
                "risk_level": r.risk_level,
                "proposed_addendum": r.proposed_prompt,  # column renamed for clarity in API
                "previous_prompt_snapshot": r.previous_prompt_snapshot,
                "sample_size": int(r.sample_size or 0),
                "low_confidence": bool(r.low_confidence),
                "status": r.status,
                "approved_by": r.approved_by,
                "approved_at": r.approved_at,
                "created_at": r.created_at,
            }
            for r in rows
        ]


# Re-export the constant the UI uses for its "Not enough data" copy.
MIN_SAMPLE_FOR_RECOMMENDATION = SAMPLE_WAIT_MAX
