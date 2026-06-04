"""Cold call script generation with Sonnet 4.6."""
import json
import logging
import time

from app.lib.config import get_secret

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.config import settings
from src.context import format_lead_context
from src.db import session_scope
from src.icp_config import load_workspace_icp_config
from src.llm import generate_json
from src.models import Enrichment, GeneratedContent, Lead, Score
from src.content.winners import load_top_negatives, load_top_winners_for
from src.prompts.call_script import PROMPT_VERSION, build_system as build_call_system
from src.prompts.sanitize import sanitize_generated_text

log = logging.getLogger(__name__)


class Objection(BaseModel):
    objection: str
    response: str


class CallScript(BaseModel):
    opener: str = Field(min_length=1)
    value_prop: str = Field(min_length=1)
    objections: list[Objection] = Field(min_length=3, max_length=3)
    close: str = Field(min_length=1)


async def generate_call_script(
    lead_id: int,
    *,
    regeneration_feedback: str | None = None,
    workspace_id: int | None = None,
) -> CallScript:
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
        user_msg += "\n\nWrite the call script now. Output JSON only."

    winners = load_top_winners_for("call_script", k=3)
    negatives = load_top_negatives("call_script", k=2)
    icp = load_workspace_icp_config(workspace_id)
    sender_first_name = get_secret("SENDER_FIRST_NAME", "Mohammed")
    system = build_call_system(winners, negatives, icp, sender_first_name=sender_first_name, workspace_id=workspace_id)
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
        schema=CallScript,
        max_tokens=1500,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    raw_body = json.dumps(result.model_dump(), indent=2)
    clean_body = sanitize_generated_text(raw_body, sender_first_name)

    with session_scope() as session:
        session.add(GeneratedContent(
            lead_id=lead_id,
            kind="call_script",
            subject=None,
            body=clean_body,
            signals_cited=[],
            prompt_version=PROMPT_VERSION,
            model=settings.content_model,
            workspace_id=workspace_id,
        ))

    log.info("call_script_generated", extra={
        "lead_id": lead_id, "duration_ms": duration_ms,
    })
    return result
