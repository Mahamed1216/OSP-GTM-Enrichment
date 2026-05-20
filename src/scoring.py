"""Lead scoring with Claude Opus 4.7."""
import logging
import time
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.config import settings
from src.context import format_lead_context
from src.db import session_scope
from src.icp_config import load_icp_config
from src.llm import generate_json
from src.models import Enrichment, Lead, Score
from src.prompts.scoring import build_system as build_scoring_system

log = logging.getLogger(__name__)


class ScoreResult(BaseModel):
    score: int = Field(ge=1, le=100)
    tier: Literal["A", "B", "C"]
    rationale: str = Field(min_length=1)
    signals_used: list[str] = Field(default_factory=list)


async def score_lead(lead_id: int) -> ScoreResult:
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        enrichment = session.execute(
            select(Enrichment).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none()
        user_msg = format_lead_context(lead, enrichment, score=None, include_score=False)

    icp = load_icp_config()
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

    # Reconcile tier with config thresholds — single source of truth lives in config.
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
        else:
            session.add(Score(
                lead_id=lead_id,
                score=result.score,
                tier=result.tier,
                rationale=result.rationale,
                signals_used=result.signals_used,
                model=settings.scoring_model,
            ))

    log.info("scoring_complete", extra={
        "lead_id": lead_id,
        "score": result.score,
        "tier": result.tier,
        "duration_ms": duration_ms,
    })
    return result
