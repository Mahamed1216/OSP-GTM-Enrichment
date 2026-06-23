"""DB persistence for lead signals + applying the hiring tier-uplift.

Kept separate from the research module (hiring.py) so the scoring path can
import `apply_hiring_uplift` without pulling in the Tavily/LLM dependencies.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from src.config import settings
from src.db import session_scope
from src.models import LeadSignal, Score, now_utc
from src.signals.schemas import HiringSignalResult
from src.signals.uplift import count_high_weight_roles, resolve_uplift

log = logging.getLogger(__name__)

HIRING = "hiring"
SOURCE_IMPORT = "source_import"


def get_source_signal(lead_id: int) -> dict | None:
    """Return the imported source-signal LeadSignal for a lead, as a dict."""
    with session_scope() as session:
        row = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == SOURCE_IMPORT,
            )
        ).scalar_one_or_none()
        return _to_dict(row) if row is not None else None


def has_source_signal(lead_id: int) -> bool:
    with session_scope() as session:
        return session.execute(
            select(LeadSignal.id).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == SOURCE_IMPORT,
            )
        ).scalar_one_or_none() is not None


def upsert_source_signal(
    lead_id: int,
    parsed: dict,
    *,
    workspace_id: int | None = None,
) -> dict | None:
    """Persist parsed OSP Lead Engine signals for a lead. Idempotent.

    `parsed` is a ParsedSourceSignals.as_dict(). Stores a normalized
    lead_signals row (signal_type="source_import") AND the colleague's tier /
    tier_score on the Lead row (only when those Lead columns are still empty —
    field promotion, never overwrite). Returns the stored row dict, or None
    when the payload carried no signals.

    Does NOT touch the Score row — apply_signal_uplifts does that.
    """
    has_signals = bool(parsed.get("has_signals"))

    with session_scope() as session:
        from src.models import Lead

        lead = session.get(Lead, lead_id)
        if lead is not None:
            # Field promotion: set source tier/score only if not already set.
            if parsed.get("source_tier") and not lead.source_tier:
                lead.source_tier = str(parsed["source_tier"])[:16]
            if parsed.get("source_tier_score") is not None and lead.source_tier_score is None:
                lead.source_tier_score = float(parsed["source_tier_score"])
            if workspace_id is None:
                workspace_id = lead.workspace_id

        if not has_signals:
            return None

        row = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == SOURCE_IMPORT,
            )
        ).scalar_one_or_none()
        if row is None:
            row = LeadSignal(lead_id=lead_id, signal_type=SOURCE_IMPORT, workspace_id=workspace_id)
            session.add(row)
        elif workspace_id is not None:
            row.workspace_id = workspace_id

        signals = parsed.get("signals") or []
        urls = [s.get("signal_source_url") for s in signals if s.get("signal_source_url")]
        summary = parsed.get("summary") or ""

        row.signal_found = True
        row.signal_strength = parsed.get("overall_strength") or "none"
        # relevant_roles holds the STRONG signal names — _signal_relevant_count
        # uses len() of this for the multi-signal C→A / B→A bump.
        row.relevant_roles = list(parsed.get("strong_signal_names") or [])
        row.relevant_departments = list(parsed.get("matched_icps") or [])
        row.summary = summary or None
        row.why_it_matters = summary or None
        row.source_urls = urls
        row.recommended_email_angle = _best_signal_angle(signals)
        row.tier_uplift_recommendation = "none"
        row.raw_payload = parsed
        row.status = "completed"
        row.last_run_at = now_utc()
        row.updated_at = now_utc()
        session.flush()
        return _to_dict(row)


def _best_signal_angle(signals: list[dict]) -> str | None:
    """Pick the strongest signal's summary/name as a personalization angle."""
    if not signals:
        return None
    rank = {"high": 3, "medium": 2, "low": 1, "none": 0}
    best = max(signals, key=lambda s: rank.get((s.get("signal_strength") or "low"), 1))
    return best.get("signal_summary") or best.get("signal_name") or best.get("signal_type")


def get_hiring_signal(lead_id: int) -> dict | None:
    """Return the hiring LeadSignal for a lead as a plain dict, or None."""
    with session_scope() as session:
        row = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == HIRING,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _to_dict(row)


def has_hiring_signal(lead_id: int) -> bool:
    with session_scope() as session:
        return session.execute(
            select(LeadSignal.id).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == HIRING,
            )
        ).scalar_one_or_none() is not None


def _to_dict(row: LeadSignal) -> dict:
    return {
        "id": row.id,
        "lead_id": row.lead_id,
        "workspace_id": row.workspace_id,
        "signal_type": row.signal_type,
        "signal_found": bool(row.signal_found),
        "signal_strength": row.signal_strength,
        "relevant_roles": list(row.relevant_roles or []),
        "relevant_departments": list(row.relevant_departments or []),
        "recency_estimate": row.recency_estimate,
        "summary": row.summary,
        "why_it_matters": row.why_it_matters,
        "source_urls": list(row.source_urls or []),
        "recommended_email_angle": row.recommended_email_angle,
        "tier_uplift_recommendation": row.tier_uplift_recommendation,
        "applied_uplift": row.applied_uplift,
        "base_tier": row.base_tier,
        "base_score": row.base_score,
        "status": row.status,
        "last_run_at": row.last_run_at,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def upsert_hiring_signal(
    lead_id: int,
    result: HiringSignalResult,
    *,
    workspace_id: int | None = None,
    summary: str | None = None,
    status: str = "completed",
    error: str | None = None,
) -> dict:
    """Create or update the hiring LeadSignal row for a lead. Idempotent.

    Stores the classifier output plus run bookkeeping (status, last_run_at,
    error). Does NOT touch the Score row — call apply_hiring_uplift for that.
    """
    with session_scope() as session:
        row = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == HIRING,
            )
        ).scalar_one_or_none()
        if row is None:
            row = LeadSignal(lead_id=lead_id, signal_type=HIRING, workspace_id=workspace_id)
            session.add(row)
        elif workspace_id is not None:
            row.workspace_id = workspace_id

        row.signal_found = bool(result.hiring_signal_found)
        row.signal_strength = result.hiring_signal_strength
        row.relevant_roles = list(result.relevant_roles_found or [])
        row.relevant_departments = list(result.relevant_departments or [])
        row.recency_estimate = result.recency_estimate
        row.why_it_matters = result.why_it_matters or None
        row.summary = summary or result.why_it_matters or None
        row.source_urls = list(result.source_urls or [])
        row.recommended_email_angle = result.recommended_email_angle or None
        row.tier_uplift_recommendation = result.tier_uplift_recommendation
        row.raw_payload = result.model_dump()
        row.status = status
        row.error = error
        row.last_run_at = now_utc()
        row.updated_at = now_utc()
        session.flush()
        return _to_dict(row)


def record_hiring_failure(
    lead_id: int,
    error: str,
    *,
    workspace_id: int | None = None,
) -> dict:
    """Record a failed hiring-enrichment run (status="failed" + error).

    Creates the row if absent; preserves any previously-found signal fields
    on an existing row (only the status/error/last_run_at are stamped) so a
    transient failure doesn't wipe out good prior research.
    """
    with session_scope() as session:
        row = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == HIRING,
            )
        ).scalar_one_or_none()
        if row is None:
            row = LeadSignal(lead_id=lead_id, signal_type=HIRING, workspace_id=workspace_id)
            session.add(row)
        elif workspace_id is not None:
            row.workspace_id = workspace_id
        row.status = "failed"
        row.error = error
        row.last_run_at = now_utc()
        row.updated_at = now_utc()
        session.flush()
        return _to_dict(row)


_RATIONALE_MARKER = "\n\nSignal uplift:"
_UPLIFT_PREFIXES = ("hiring:", "source:")
_TIER_RANK = {"D": 0, "C": 1, "B": 2, "A": 3}


def _signal_relevant_count(signal: LeadSignal) -> int:
    """How many relevant items back the signal (drives the multi-bump rules).

    Hiring: count of high-weight roles among relevant_roles.
    Other (source_import): count of strong signal names in relevant_roles.
    """
    if signal.signal_type == HIRING:
        return count_high_weight_roles(signal.relevant_roles or [])
    return len([r for r in (signal.relevant_roles or []) if r])


def _marker_prefix(signal_type: str) -> str:
    return "source" if signal_type == SOURCE_IMPORT else "hiring"


def apply_signal_uplifts(
    lead_id: int,
    *,
    workspace_id: int | None = None,
    base_tier: str | None = None,
    base_score: int | None = None,
) -> dict | None:
    """Apply the best deterministic signal tier-uplift to a lead's Score row.

    Considers ALL signal rows for the lead (hiring + source_import) and applies
    the single strongest qualifying uplift, so hiring and imported source
    signals never compound on top of each other.

    Idempotent. The "base" (pre-uplift) tier/score is:
      1. `base_tier`/`base_score` args when provided (scoring passes the fresh
         LLM base so uplifts never compound), else
      2. a base stored on any signal row from a previous apply, else
      3. the current Score row.

    Returns a summary dict for the winning uplift (or recommendation="none"),
    or None when there are no signals / no Score row. NEVER sends or pushes.
    """
    with session_scope() as session:
        signals = session.execute(
            select(LeadSignal).where(LeadSignal.lead_id == lead_id)
        ).scalars().all()
        if not signals:
            return None

        score = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        if score is None:
            return None

        # Effective base (see docstring precedence).
        if base_tier is not None and base_score is not None:
            eff_base_tier, eff_base_score = base_tier, int(base_score)
        else:
            stored = next(
                (s for s in signals if s.base_tier is not None and s.base_score is not None),
                None,
            )
            if stored is not None:
                eff_base_tier, eff_base_score = stored.base_tier, int(stored.base_score)
            else:
                eff_base_tier, eff_base_score = score.tier, int(score.score)

        # Compute a candidate uplift per signal; persist base + recommendation.
        best = None  # tuple(outcome, signal)
        for sig in signals:
            sig.base_tier = eff_base_tier
            sig.base_score = eff_base_score
            outcome = resolve_uplift(
                base_tier=eff_base_tier,
                base_score=eff_base_score,
                strength=sig.signal_strength,
                relevant_role_count=_signal_relevant_count(sig),
                tier_a_min=settings.tier_a_min,
                tier_b_min=settings.tier_b_min,
            )
            sig.tier_uplift_recommendation = outcome.recommendation
            sig.applied_uplift = "none"
            if outcome.applied:
                if best is None or _TIER_RANK.get(outcome.new_tier, 0) > _TIER_RANK.get(best[0].new_tier, 0):
                    best = (outcome, sig)

        # Reset to base, strip prior uplift markers + note, then re-apply best.
        score.tier = eff_base_tier
        score.score = eff_base_score
        signals_used = [
            s for s in (score.signals_used or [])
            if not str(s).startswith(_UPLIFT_PREFIXES)
        ]
        if score.rationale and _RATIONALE_MARKER in score.rationale:
            score.rationale = score.rationale.split(_RATIONALE_MARKER)[0]

        if best is not None:
            outcome, sig = best
            sig.applied_uplift = outcome.recommendation
            score.tier = outcome.new_tier
            score.score = outcome.new_score
            kind = "hiring" if sig.signal_type == HIRING else "source"
            detail = ", ".join((sig.relevant_roles or [])[:4]) or (sig.summary or "signal")
            marker = (
                f"{_marker_prefix(sig.signal_type)}: "
                f"{outcome.recommendation.replace('_', '→')} uplift "
                f"({sig.signal_strength} {kind} signal: {detail})"
            )
            signals_used.append(marker)
            note = (
                f"{_RATIONALE_MARKER} {eff_base_tier} ({eff_base_score}) "
                f"→ {outcome.new_tier} ({outcome.new_score}). "
                f"{sig.signal_strength.title()} {kind} signal — {detail}. "
                f"{sig.why_it_matters or sig.summary or ''}"
            ).rstrip()
            score.rationale = (score.rationale or "") + note

        score.signals_used = signals_used
        session.flush()

        result_outcome = best[0] if best else None
        log.info(
            "signal_uplift_applied",
            extra={
                "lead_id": lead_id,
                "recommendation": result_outcome.recommendation if result_outcome else "none",
                "applied": bool(best),
                "base_tier": eff_base_tier,
                "new_tier": score.tier,
            },
        )
        return {
            "recommendation": result_outcome.recommendation if result_outcome else "none",
            "applied": bool(best),
            "base_tier": eff_base_tier,
            "base_score": eff_base_score,
            "new_tier": score.tier,
            "new_score": int(score.score),
        }


# Backward-compatible alias. Existing callers (scoring, rescue, Lead Detail,
# tests) call apply_hiring_uplift; it now considers all signal types via the
# unified path so hiring + source signals never double-bump.
def apply_hiring_uplift(
    lead_id: int,
    *,
    workspace_id: int | None = None,
    base_tier: str | None = None,
    base_score: int | None = None,
) -> dict | None:
    return apply_signal_uplifts(
        lead_id, workspace_id=workspace_id, base_tier=base_tier, base_score=base_score,
    )
