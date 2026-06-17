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


def apply_hiring_uplift(
    lead_id: int,
    *,
    workspace_id: int | None = None,
    base_tier: str | None = None,
    base_score: int | None = None,
) -> dict | None:
    """Apply the deterministic hiring tier-uplift to a lead's Score row.

    Idempotent. The "base" (pre-uplift) tier/score used for the rule is:
      1. `base_tier`/`base_score` args when provided (the scoring path passes
         the freshly-computed LLM base so the uplift never compounds), else
      2. the base stored on the LeadSignal from a previous apply, else
      3. the current Score row (first standalone apply).

    Returns a small summary dict, or None when there is nothing to do
    (no hiring signal, or no Score row yet).

    NEVER sends or pushes anything — it only rewrites the local Score row.
    """
    with session_scope() as session:
        signal = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == HIRING,
            )
        ).scalar_one_or_none()
        if signal is None:
            return None

        score = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        if score is None:
            return None

        # Resolve the base to compute against (see docstring precedence).
        if base_tier is not None and base_score is not None:
            eff_base_tier, eff_base_score = base_tier, int(base_score)
        elif signal.base_tier is not None and signal.base_score is not None:
            eff_base_tier, eff_base_score = signal.base_tier, int(signal.base_score)
        else:
            eff_base_tier, eff_base_score = score.tier, int(score.score)

        # Persist the base for future idempotent re-applies.
        signal.base_tier = eff_base_tier
        signal.base_score = eff_base_score

        outcome = resolve_uplift(
            base_tier=eff_base_tier,
            base_score=eff_base_score,
            strength=signal.signal_strength,
            relevant_role_count=count_high_weight_roles(signal.relevant_roles or []),
            tier_a_min=settings.tier_a_min,
            tier_b_min=settings.tier_b_min,
        )
        signal.tier_uplift_recommendation = outcome.recommendation
        signal.applied_uplift = outcome.recommendation if outcome.applied else "none"

        # Always reset to base first, then re-apply, so repeated calls are stable.
        score.tier = eff_base_tier
        score.score = eff_base_score
        signals_used = [
            s for s in (score.signals_used or [])
            if not str(s).startswith("hiring:")
        ]

        if outcome.applied:
            score.tier = outcome.new_tier
            score.score = outcome.new_score
            roles = ", ".join((signal.relevant_roles or [])[:4]) or "relevant roles"
            marker = (
                f"hiring: {outcome.recommendation.replace('_', '→')} uplift "
                f"({signal.signal_strength} hiring signal: {roles})"
            )
            signals_used.append(marker)
            note = (
                f"\n\nHiring-signal rescue: {eff_base_tier} ({eff_base_score}) "
                f"→ {outcome.new_tier} ({outcome.new_score}). "
                f"{signal.signal_strength.title()} hiring signal — {roles}. "
                f"{signal.why_it_matters or ''}"
            ).rstrip()
            if "Hiring-signal rescue:" not in (score.rationale or ""):
                score.rationale = (score.rationale or "") + note
            else:
                # Strip any prior rescue note then re-append the fresh one.
                base_rationale = (score.rationale or "").split("\n\nHiring-signal rescue:")[0]
                score.rationale = base_rationale + note

        else:
            # Recommendation is none — make sure no stale rescue note lingers.
            if score.rationale and "Hiring-signal rescue:" in score.rationale:
                score.rationale = score.rationale.split("\n\nHiring-signal rescue:")[0]

        score.signals_used = signals_used
        session.flush()

        log.info(
            "hiring_uplift_applied",
            extra={
                "lead_id": lead_id,
                "recommendation": outcome.recommendation,
                "applied": outcome.applied,
                "base_tier": eff_base_tier,
                "new_tier": outcome.new_tier,
            },
        )
        return {
            "recommendation": outcome.recommendation,
            "applied": outcome.applied,
            "base_tier": eff_base_tier,
            "base_score": eff_base_score,
            "new_tier": outcome.new_tier,
            "new_score": outcome.new_score,
        }
