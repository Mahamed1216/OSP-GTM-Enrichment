"""Pipeline orchestrator: one lead from DB row to delivered email.

Stages (per lead, sequential across leads):
  1. enrich  — fan-out 6 sources via asyncio.gather
  2. score   — Claude Opus 4.7
  3. generate (parallel) — email + call script + LinkedIn DM via Sonnet 4.6
                           SKIPPED when tier < SEND_MIN_TIER (saves LLM tokens)
  4. deliver — Instantly v2 with pre-send guards (tier, dedupe, verify, send)
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from src.config import settings
from src.content.call_script import CallScript, generate_call_script
from src.content.email import EmailResult, generate_email
from src.content.linkedin_msg import LinkedInMessage, generate_linkedin_msg
from src.db import session_scope
from src.delivery.instantly import DeliveryResult, deliver_email
from src.enrichment.waterfall import enrich_lead
from src.icp_config import load_icp_config
from src.models import Lead
from src.scoring import ScoreResult, score_lead

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    lead_id: int
    enrichment_status: dict = field(default_factory=dict)
    score: Optional[ScoreResult] = None
    email: Optional[EmailResult] = None
    call_script: Optional[CallScript] = None
    linkedin_msg: Optional[LinkedInMessage] = None
    delivery: Optional[DeliveryResult] = None
    error: Optional[str] = None
    duration_ms: int = 0
    content_skipped: bool = False  # True when tier was below threshold


async def run_pipeline_for_lead(lead_id: int, *, dry_run: bool = True) -> PipelineResult:
    result = PipelineResult(lead_id=lead_id)
    start = time.monotonic()
    try:
        # Single ICP load at pipeline entry — only the override boolean
        # needs to be consistent for the duration of this run. Downstream
        # functions (score_lead, generate_*, enrich_lead) keep their own
        # per-call loads; see Phase 6 plan tradeoff (c).
        icp = load_icp_config()

        result.enrichment_status = await enrich_lead(lead_id)
        result.score = await score_lead(lead_id)

        send_gate_open = settings.should_send(result.score.tier)
        tier_gate_open = icp.generate_content_for_all_tiers or send_gate_open

        if not tier_gate_open:
            result.content_skipped = True
            log.info("pipeline_content_skipped_tier", extra={
                "lead_id": lead_id, "tier": result.score.tier, "score": result.score.score,
            })
        else:
            if icp.generate_content_for_all_tiers and not send_gate_open:
                log.info("pipeline_tier_override_active", extra={
                    "lead_id": lead_id, "tier": result.score.tier, "score": result.score.score,
                })
            email, call_script, dm = await asyncio.gather(
                generate_email(lead_id),
                generate_call_script(lead_id),
                generate_linkedin_msg(lead_id),
            )
            result.email = email
            result.call_script = call_script
            result.linkedin_msg = dm

            # Delivery stays gated on SEND_MIN_TIER — the override only
            # unlocks content generation, never sends to an out-of-tier prospect.
            if send_gate_open:
                result.delivery = await deliver_email(lead_id, dry_run=dry_run)

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("pipeline_failed", extra={"lead_id": lead_id})
    finally:
        result.duration_ms = int((time.monotonic() - start) * 1000)
        log.info("pipeline_complete", extra={
            "lead_id": lead_id,
            "duration_ms": result.duration_ms,
            "ok": result.error is None,
            "tier": result.score.tier if result.score else None,
        })
    return result


async def run_pipeline_for_all(*, dry_run: bool = True) -> list[PipelineResult]:
    """Process every lead in the DB sequentially."""
    with session_scope() as session:
        ids = [lid for (lid,) in session.query(Lead.id).all()]

    results: list[PipelineResult] = []
    for lid in ids:
        results.append(await run_pipeline_for_lead(lid, dry_run=dry_run))
    return results
