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
from src.prompts.sanitize import sanitize_generated_text

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

    with session_scope() as session:
        session.add(GeneratedContent(
            lead_id=lead_id,
            kind="email",
            subject=clean_subject,
            body=clean_body,
            signals_cited=result.signals_cited,
            prompt_version=PROMPT_VERSION,
            prompt_fingerprint=current_email_prompt_fingerprint(),
            model=settings.content_model,
        ))

    log.info("email_generated", extra={
        "lead_id": lead_id,
        "duration_ms": duration_ms,
        "signals_count": len(result.signals_cited),
        "subject_chars": len(result.subject),
        "body_chars": len(result.body),
    })
    return result
