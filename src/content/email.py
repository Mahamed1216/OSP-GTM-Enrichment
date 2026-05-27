"""Email generation with Sonnet 4.6 + few-shot from winning examples."""
import logging
import time

from app.lib.config import get_secret

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.config import settings
from src.context import format_lead_context
from src.content.winners import load_top_negatives, load_top_winners_for
from src.db import session_scope
from src.icp_config import load_icp_config
from src.llm import generate_json
from src.models import Enrichment, GeneratedContent, Lead, Score
from src.prompts.email import (
    PROMPT_VERSION,
    build_system as build_email_system,
    current_email_prompt_fingerprint,
)
from src.prompts.sanitize import (
    detect_competitor_as_buyer,
    detect_segment_vs_company_mismatch,
    sanitize_generated_text,
)

log = logging.getLogger(__name__)


class EmailResult(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    signals_cited: list[str] = Field(default_factory=list)


async def generate_email(
    lead_id: int,
    *,
    regeneration_feedback: str | None = None,
) -> EmailResult:
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        enrichment = session.execute(
            select(Enrichment).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none()
        score = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        user_msg = format_lead_context(lead, enrichment, score)
        user_msg += "\n\nWrite the email now. Output JSON only."
        # Snapshot buyer-account fields for the post-generation validator.
        # Read INSIDE the session so we never touch detached attributes.
        ba = (enrichment.buyer_accounts or {}) if enrichment else {}
        named_buyers: list[str] = list(ba.get("likely_buyer_accounts") or [])
        flagged_competitors: list[str] = list(ba.get("flagged_competitors") or [])
        company_industry = lead.industry or None

    winners = load_top_winners_for("email", k=3)
    negatives = load_top_negatives("email", k=2)
    icp = load_icp_config()
    sender_first_name = get_secret("SENDER_FIRST_NAME", "Mohammed")
    system = build_email_system(winners, negatives, icp, sender_first_name=sender_first_name)
    if regeneration_feedback:
        system = (
            "Previous version of this content was rated negatively. "
            f"Reason: {regeneration_feedback.strip()}. Address this in your output.\n\n"
            + system
        )

    start = time.monotonic()
    result = await generate_json(
        model=settings.content_model,
        system=system,
        user=user_msg,
        schema=EmailResult,
        max_tokens=1000,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    clean_subject = sanitize_generated_text(result.subject, sender_first_name)
    clean_body = sanitize_generated_text(result.body, sender_first_name)

    # Post-generation validators. These are HEURISTIC flags, not hard
    # blocks — the email still saves, but warnings ride along on
    # `signals_cited` so the operator can review on the Lead detail page
    # and choose to regenerate.
    validation_warnings: list[str] = []
    seg_warnings = detect_segment_vs_company_mismatch(
        clean_body, named_buyer_accounts=named_buyers,
    )
    validation_warnings.extend(seg_warnings)
    competitor_hits = detect_competitor_as_buyer(
        clean_body,
        company_industry=company_industry,
        competitor_seed=flagged_competitors,
    )
    if competitor_hits:
        validation_warnings.append(
            "Possible competitor named as buyer: " + ", ".join(competitor_hits)
        )
    # Attach as prefixed entries to signals_cited so existing UIs that
    # render the field show them without a schema change. Prefix makes
    # them filterable downstream.
    signals_with_warnings = list(result.signals_cited or []) + [
        f"validator:{w}" for w in validation_warnings
    ]

    with session_scope() as session:
        session.add(GeneratedContent(
            lead_id=lead_id,
            kind="email",
            subject=clean_subject,
            body=clean_body,
            signals_cited=signals_with_warnings,
            prompt_version=PROMPT_VERSION,
            prompt_fingerprint=current_email_prompt_fingerprint(),
            model=settings.content_model,
        ))

    if validation_warnings:
        log.warning(
            "email_validation_warnings",
            extra={"lead_id": lead_id, "warnings": validation_warnings},
        )

    log.info("email_generated", extra={
        "lead_id": lead_id,
        "duration_ms": duration_ms,
        "signals_count": len(result.signals_cited),
        "subject_chars": len(result.subject),
        "body_chars": len(result.body),
    })
    return result
