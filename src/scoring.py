"""Lead scoring with Claude Opus 4.7."""
import logging
import time
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.config import settings
from src.context import format_lead_context
from src.db import session_scope
from src.icp_config import load_workspace_icp_config
from src.llm import generate_json
from src.models import Enrichment, Lead, Score, now_utc
from src.prompts.scoring import build_system as build_scoring_system

log = logging.getLogger(__name__)


class ScoreResult(BaseModel):
    # Tier widened to include "D" — the ICP-skip tier for confirmed
    # B2C / non-fit leads. The LLM never returns D directly; it's
    # produced by the pre-scoring B2C-fit gate below.
    score: int = Field(ge=1, le=100)
    tier: Literal["A", "B", "C", "D"]
    rationale: str = Field(min_length=1)
    signals_used: list[str] = Field(default_factory=list)


# The canned tier-D score we stamp when a B2C-without-B2B-motion lead is
# detected. Fixed text so the email-skip path and the Lead Detail
# warning banner can both pattern-match on it.
B2C_SKIP_SCORE = 20
B2C_SKIP_RATIONALE = (
    "Primary motion appears B2C. OSP is a B2B outbound agency, so this "
    "is not a fit unless there is clear B2B partnership or enterprise motion."
)
B2C_SKIP_SIGNAL = "B2C motion, poor OSP fit"


def _is_b2c_without_b2b_motion(buyer_accounts: dict | None) -> tuple[bool, list[str]]:
    """Return (should_skip, evidence_present).

    Skip = True when buyer_motion == "B2C" AND the discovery's
    explicit_b2b_motion_evidence list is empty. Any non-empty evidence
    list keeps the lead in the normal scoring path — even one
    employer-benefits page or partner-program reference qualifies it.
    """
    if not buyer_accounts:
        return False, []
    motion = (buyer_accounts.get("buyer_motion") or "").upper()
    if motion != "B2C":
        return False, []
    evidence = list(buyer_accounts.get("explicit_b2b_motion_evidence") or [])
    return (not evidence), evidence


def _persist_skip_score(lead_id: int) -> ScoreResult:
    """Write the canned tier-D Score row (upsert) and return it."""
    result = ScoreResult(
        score=B2C_SKIP_SCORE,
        tier="D",
        rationale=B2C_SKIP_RATIONALE,
        signals_used=[B2C_SKIP_SIGNAL],
    )
    with session_scope() as session:
        existing = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        if existing:
            existing.score = result.score
            existing.tier = result.tier
            existing.rationale = result.rationale
            existing.signals_used = result.signals_used
            existing.model = "rule:b2c_skip"
            existing.scored_at = now_utc()
        else:
            session.add(Score(
                lead_id=lead_id,
                score=result.score,
                tier=result.tier,
                rationale=result.rationale,
                signals_used=result.signals_used,
                model="rule:b2c_skip",
            ))
    log.info(
        "scoring_b2c_skip",
        extra={"lead_id": lead_id, "score": result.score, "tier": "D"},
    )
    return result


async def score_lead(lead_id: int, *, workspace_id: int | None = None) -> ScoreResult:
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        enrichment = session.execute(
            select(Enrichment).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none()
        # Snapshot buyer_accounts inside the session — the pre-scoring
        # gate runs AFTER the session closes.
        buyer_accounts = (enrichment.buyer_accounts or {}) if enrichment else {}
        user_msg = format_lead_context(lead, enrichment, score=None, include_score=False)

    # ICP gate: confirmed B2C without B2B motion → tier D, skip the LLM.
    # OSP is a B2B outbound agency; spending tokens on a guaranteed
    # poor-fit lead is waste. The deterministic gate also makes the
    # outcome explainable on the Lead Detail page ("model:rule:b2c_skip"
    # is shown next to the score so the operator can tell it apart from
    # a normal LLM-produced score).
    should_skip, _ = _is_b2c_without_b2b_motion(buyer_accounts)
    if should_skip:
        return _persist_skip_score(lead_id)

    icp = load_workspace_icp_config(workspace_id)
    system = build_scoring_system(icp)

    start = time.monotonic()
    result = await generate_json(
        model=settings.scoring_model,
        system=system,
        user=user_msg,
        schema=ScoreResult,
        max_tokens=800,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    # Reconcile tier with config thresholds — single source of truth
    # lives in config. Tier D is reserved for the pre-LLM B2C-skip gate;
    # the LLM never produces it directly.
    if result.tier != "D":
        expected_tier = settings.tier_for_score(result.score)
        if result.tier != expected_tier:
            log.info("scoring_tier_reconciled", extra={
                "lead_id": lead_id, "llm_tier": result.tier, "config_tier": expected_tier,
            })
            result.tier = expected_tier

    with session_scope() as session:
        existing = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        if existing:
            existing.score = result.score
            existing.tier = result.tier
            existing.rationale = result.rationale
            existing.signals_used = result.signals_used
            existing.model = settings.scoring_model
            existing.scored_at = now_utc()
        else:
            session.add(Score(
                lead_id=lead_id,
                score=result.score,
                tier=result.tier,
                rationale=result.rationale,
                signals_used=result.signals_used,
                model=settings.scoring_model,
                workspace_id=workspace_id,
            ))

    # Hiring-signal tier uplift (C-tier rescue layer). Applied AFTER the base
    # LLM score so the deterministic rule operates on a clean base. No-op when
    # the lead has no hiring signal stored. Wrapped so scoring never breaks if
    # the lead_signals table is missing (pre-migration DB).
    try:
        from src.signals.store import apply_hiring_uplift
        uplift = apply_hiring_uplift(
            lead_id,
            workspace_id=workspace_id,
            base_tier=result.tier,
            base_score=result.score,
        )
        if uplift and uplift.get("applied"):
            result.tier = uplift["new_tier"]
            result.score = uplift["new_score"]
            log.info("scoring_hiring_uplift", extra={
                "lead_id": lead_id,
                "recommendation": uplift["recommendation"],
                "new_tier": result.tier,
            })
    except Exception as exc:  # pragma: no cover - uplift is best-effort
        log.warning("scoring_hiring_uplift_failed", extra={"lead_id": lead_id, "error": str(exc)})

    log.info("scoring_complete", extra={
        "lead_id": lead_id,
        "score": result.score,
        "tier": result.tier,
        "duration_ms": duration_ms,
    })
    return result
