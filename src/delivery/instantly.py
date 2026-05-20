"""Instantly v2 delivery: pre-send guards + add lead to campaign.

Pre-send guards run in this order:
  1. Score tier must meet SEND_MIN_TIER
  2. Dedupe — skip if a delivered email already exists for this lead
  3. Email verification (cached) — skip on invalid (and on risky if strict)
  4. Send (or log dry-run payload)
"""
import logging
import time
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import select

from src.config import settings
from src.db import session_scope
from src.delivery.verify_email import verify_email
from src.models import GeneratedContent, Lead, Score
from src.retry import retry_api

log = logging.getLogger(__name__)

SkipReason = Literal[
    "tier_below_threshold",
    "already_delivered",
    "email_invalid",
    "no_email_content",
]


class DeliveryResult(BaseModel):
    delivered: bool
    skip_reason: SkipReason | None = None
    delivery_id: str | None = None
    dry_run: bool = False


_CLIENT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_API_BASE = "https://api.instantly.ai/api/v2"


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.instantly_api_key}",
        "Content-Type": "application/json",
    }


@retry_api
async def get_campaign(campaign_id: str) -> dict:
    """GET /api/v2/campaigns/{id}. Raises httpx.HTTPStatusError on 401/403/404."""
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/campaigns/{campaign_id}",
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()


@retry_api
async def count_campaign_leads(campaign_id: str) -> int | None:
    """Return number of leads currently in the campaign, or None if the
    API shape doesn't expose a usable total (script renders 'unknown')."""
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.post(
            f"{_API_BASE}/leads/list",
            headers=_auth_headers(),
            json={"campaign": campaign_id, "limit": 1},
        )
        resp.raise_for_status()
        data = resp.json()
    for key in ("total", "total_count", "count"):
        val = data.get(key)
        if isinstance(val, int):
            return val
    items = data.get("items") or data.get("data") or data.get("leads")
    if isinstance(items, list) and not data.get("next_starting_after"):
        return len(items)
    return None


def _accept_verification(status: str, strict: bool = False) -> bool:
    if status == "valid":
        return True
    if status == "invalid":
        return False
    # risky / unknown
    return not strict


@retry_api
async def get_lead(remote_lead_id: str) -> dict:
    """GET /api/v2/leads/{id}. Used by the push-one smoke to read back the
    custom_variables that landed on Instantly's side."""
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/leads/{remote_lead_id}",
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()


@retry_api
async def _post_lead_to_campaign(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.post(
            f"{_API_BASE}/leads",
            headers=_auth_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def _build_payload(lead: Lead, content: GeneratedContent) -> dict:
    """Build the Instantly /api/v2/leads payload.

    Body + subject are passed via custom_variables. The campaign sequence
    template MUST reference {{personalized_subject}} and {{personalized_body}}
    — without those placeholders Instantly falls back to its hardcoded
    template content and the generated copy never reaches the recipient.

    Top-level field is `campaign` (not `campaign_id`) per Instantly v2 API.
    """
    return {
        "campaign": settings.instantly_campaign_id,
        "email": lead.email,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company_name": lead.company,
        "personalization": content.body,
        "custom_variables": {
            "personalized_subject": content.subject or "",
            "personalized_body": content.body,
            "signals_cited": ", ".join(content.signals_cited or []),
        },
    }


async def deliver_email(
    lead_id: int,
    *,
    dry_run: bool = False,
    strict_verification: bool = False,
) -> DeliveryResult:
    """Run pre-send guards then deliver to Instantly."""
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")

        score = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()

        # Latest undelivered email content for this lead
        content = session.execute(
            select(GeneratedContent)
            .where(GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "email")
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()

        already_delivered = session.execute(
            select(GeneratedContent)
            .where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == "email",
                GeneratedContent.delivered_at.is_not(None),
            )
        ).scalars().first()

        # Snapshot everything we need so the guards below can run
        # without touching detached ORM instances (commit on __exit__
        # expires attributes; later access would raise DetachedInstanceError).
        lead_email = lead.email
        content_id = content.id if content else None
        tier_snapshot: str | None = score.tier if score else None
        already_delivered_flag = already_delivered is not None
        has_content = content is not None

    # Guard 1: tier
    if tier_snapshot is None or not settings.should_send(tier_snapshot):  # type: ignore[arg-type]
        return await _record_skip(content_id, "tier_below_threshold", lead_id, tier_snapshot)

    # Guard 2: dedupe
    if already_delivered_flag:
        return await _record_skip(content_id, "already_delivered", lead_id, tier_snapshot)

    # Guard 3: no content
    if not has_content:
        return await _record_skip(None, "no_email_content", lead_id, tier_snapshot)

    # Guard 4: email verification
    verify = await verify_email(lead_id)
    if not _accept_verification(verify.status, strict=strict_verification):
        return await _record_skip(content_id, "email_invalid", lead_id, tier_snapshot, verify_status=verify.status)

    # All guards passed — build payload + send
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        content = session.get(GeneratedContent, content_id)
        payload = _build_payload(lead, content)

    if dry_run:
        log.info("delivery_dry_run", extra={
            "lead_id": lead_id, "to": lead_email,
            "subject": payload["custom_variables"]["personalized_subject"],
            "campaign": payload["campaign"],
        })
        return DeliveryResult(delivered=False, dry_run=True)

    # Mark in-progress before firing — surfaces stuck sends if the process dies.
    with session_scope() as session:
        content = session.get(GeneratedContent, content_id)
        content.delivery_status = "in_progress"
        content.error_message = None

    start = time.monotonic()
    try:
        response = await _post_lead_to_campaign(payload)
    except Exception as exc:
        # @retry_api has already exhausted retries for 5xx/429 — this is terminal.
        err_text = _format_error(exc)
        with session_scope() as session:
            content = session.get(GeneratedContent, content_id)
            content.delivery_status = "error"
            content.error_message = err_text
            content.delivered_at = None
        log.warning("delivery_failed", extra={
            "lead_id": lead_id, "error": err_text,
        })
        raise

    duration_ms = int((time.monotonic() - start) * 1000)
    delivery_id = str(response.get("id") or response.get("lead_id") or "")

    with session_scope() as session:
        content = session.get(GeneratedContent, content_id)
        content.delivered_at = datetime.utcnow()
        content.delivery_provider = "instantly"
        content.delivery_id = delivery_id
        content.delivery_status = "sent"
        content.error_message = None

    log.info("delivery_complete", extra={
        "lead_id": lead_id, "delivery_id": delivery_id, "duration_ms": duration_ms,
    })
    return DeliveryResult(delivered=True, delivery_id=delivery_id)


def _format_error(exc: BaseException) -> str:
    """Compact error text for error_message column."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = getattr(resp, "text", "") or ""
        return f"HTTP {resp.status_code}: {body[:500]}"
    return f"{type(exc).__name__}: {exc}"[:500]


async def _record_skip(
    content_id: int | None,
    reason: SkipReason,
    lead_id: int,
    tier: str | None,
    verify_status: str | None = None,
) -> DeliveryResult:
    if content_id is not None:
        with session_scope() as session:
            content = session.get(GeneratedContent, content_id)
            content.skip_reason = reason

    log.info("delivery_skipped", extra={
        "lead_id": lead_id,
        "reason": reason,
        "tier": tier,
        "verify_status": verify_status,
    })
    return DeliveryResult(delivered=False, skip_reason=reason)
