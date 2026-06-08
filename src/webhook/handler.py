"""Webhook handler: validate, resolve workspace, classify reply, store in reply_threads.

Business logic only — no HTTP concerns here. The FastAPI server (server.py)
is a thin wrapper that calls handle_reply_webhook().

Hard constraints (same as Reply Agent):
- Never auto-sends a reply.
- Never modifies campaigns or outbound email generation.
- Never overwrites Instantly copy.
- Missing reply_body → missing_reply_text status, no draft generated.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.db import session_scope
from src.feedback.reply_agent import classify_and_draft_reply
from src.feedback.reply_sync import (
    STATUS_MISSING_TEXT,
    STATUS_NEEDS_REVIEW,
    STATUS_SKIPPED,
    _classification_to_status,
    _make_dedup_key,
)
from src.models import Lead, ReplyThread

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload / result models
# ---------------------------------------------------------------------------

class WebhookPayload(BaseModel):
    """Inbound JSON from the Instantly Lead Replied automation.

    extra="allow" lets Instantly add new fields without breaking the endpoint.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    event: str | None = None
    source: str | None = None
    workspace_slug: str | None = None
    campaign_id: str | None = None
    campaign_name: str | None = None
    lead_id: str | None = None        # Instantly lead UUID
    email: str = ""
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    title: str | None = None
    reply_subject: str | None = None
    reply_body: str | None = None
    reply_date: str | None = None
    thread_id: str | None = None


class WebhookResult(BaseModel):
    ok: bool
    status: str
    reply_thread_id: int | None = None
    workspace_id: int | None = None
    lead_id: int | None = None
    classification: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _resolve_workspace(
    workspace_slug: str | None,
    campaign_id: str | None,
) -> int | None:
    """Resolve workspace_id from slug → campaign_id → default (in order)."""
    from src.workspace import (
        get_default_workspace_id,
        get_workspace_by_campaign_id,
        get_workspace_by_slug,
    )
    if workspace_slug:
        ws = get_workspace_by_slug(workspace_slug)
        if ws:
            return ws["id"]
    if campaign_id:
        ws = get_workspace_by_campaign_id(campaign_id)
        if ws:
            return ws["id"]
    return get_default_workspace_id()


# ---------------------------------------------------------------------------
# Lead lookup / creation
# ---------------------------------------------------------------------------

def _find_or_create_lead(
    email: str,
    workspace_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    company: str | None = None,
    title: str | None = None,
) -> int | None:
    """Return the lead_id for (email, workspace_id), creating a minimal row if absent.

    Returns None if creation fails (e.g. DB error). The webhook continues
    without a lead_id in that case — the reply thread is still stored.
    """
    email_clean = email.lower().strip()
    try:
        with session_scope() as session:
            existing = session.execute(
                select(Lead).where(
                    Lead.email == email_clean,
                    Lead.workspace_id == workspace_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
            lead = Lead(
                email=email_clean,
                first_name=first_name or "",
                last_name=last_name or "",
                company=company or None,
                title=title or None,
                workspace_id=workspace_id,
            )
            session.add(lead)
            session.flush()
            return lead.id
    except IntegrityError:
        with session_scope() as session:
            row = session.execute(
                select(Lead).where(
                    Lead.email == email_clean,
                    Lead.workspace_id == workspace_id,
                )
            ).scalar_one_or_none()
            return row.id if row else None
    except Exception as exc:
        log.warning(
            "webhook_lead_create_failed",
            extra={"email": email_clean, "workspace_id": workspace_id, "error": str(exc)},
        )
        return None


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------

def _prospect_name(payload: WebhookPayload) -> str | None:
    parts = [p.strip() for p in (payload.first_name or "", payload.last_name or "") if p.strip()]
    return " ".join(parts) or None


async def handle_reply_webhook(payload: WebhookPayload) -> WebhookResult:
    """Process one Instantly Lead Replied webhook.

    Steps:
      1. Validate email present.
      2. Resolve workspace (slug → campaign_id → default).
      3. Find or create lead by (email, workspace_id).
      4. Build dedup key (thread_id → lead_id → email+body hash).
      5. Check for existing thread — return duplicate if found.
      6. If reply_body missing → store missing_reply_text, return.
      7. Classify reply via Reply Agent.
      8. Store classified ReplyThread. Status follows classification.
      9. Return result. Never sends anything automatically.
    """
    from src.workspace import get_calendar_link_for_workspace, get_campaign_id_for_workspace

    # 1. Validate
    email = (payload.email or "").strip().lower()
    if not email:
        return WebhookResult(ok=False, status="failed", detail="email is required")

    # 2. Resolve workspace
    ws_id = _resolve_workspace(payload.workspace_slug, payload.campaign_id)
    if ws_id is None:
        log.warning(
            "webhook_workspace_not_found",
            extra={"slug": payload.workspace_slug, "campaign_id": payload.campaign_id},
        )
        return WebhookResult(ok=False, status="failed", detail="No workspace found for this request")

    # 3. Find or create lead
    lead_db_id = _find_or_create_lead(
        email,
        ws_id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        company=payload.company,
        title=payload.title,
    )

    # 4. Campaign ID + calendar link
    campaign_id = (
        (payload.campaign_id or "").strip()
        or get_campaign_id_for_workspace(ws_id)
        or "webhook"
    )
    calendar_link = get_calendar_link_for_workspace(ws_id) or ""

    # 5. Dedup key — prefer thread_id (stable per email conversation),
    #    then Instantly lead_id, then email+body hash.
    dedup_key = _make_dedup_key(
        payload.thread_id,
        payload.lead_id,
        email,
        payload.reply_body or "",
    )

    # 6. Check dedup — return early if already processed
    with session_scope() as session:
        existing_thread = session.execute(
            select(ReplyThread).where(
                ReplyThread.workspace_id == ws_id,
                ReplyThread.campaign_id == campaign_id,
                ReplyThread.dedup_key == dedup_key,
            )
        ).scalar_one_or_none()
        if existing_thread is not None:
            return WebhookResult(
                ok=True,
                status="duplicate",
                reply_thread_id=existing_thread.id,
                workspace_id=ws_id,
                lead_id=lead_db_id,
                classification=existing_thread.classification,
                detail="Duplicate — already processed.",
            )

    raw_payload: dict[str, Any] = payload.model_dump()
    prospect_name = _prospect_name(payload)

    # 7. Handle missing reply body
    has_real_text = bool((payload.reply_body or "").strip())
    if not has_real_text:
        try:
            with session_scope() as session:
                th = ReplyThread(
                    workspace_id=ws_id,
                    lead_id=lead_db_id,
                    campaign_id=campaign_id,
                    instantly_lead_id=payload.lead_id or None,
                    prospect_email=email,
                    prospect_name=prospect_name,
                    company_name=payload.company,
                    thread_id=payload.thread_id,
                    inbound_reply_text=(
                        "[Reply text not available — received from Instantly webhook without body]"
                    ),
                    status=STATUS_MISSING_TEXT,
                    raw_payload=raw_payload,
                    dedup_key=dedup_key,
                    human_review_notes=(
                        "Webhook received from Instantly but reply_body was empty. "
                        "Check the Instantly dashboard for the actual reply body."
                    ),
                )
                session.add(th)
                session.flush()
                thread_id = th.id
        except IntegrityError:
            thread_id = _fetch_thread_id(ws_id, campaign_id, dedup_key)
        log.info("webhook_missing_reply_body", extra={"email": email, "thread_id": thread_id})
        return WebhookResult(
            ok=True,
            status=STATUS_MISSING_TEXT,
            reply_thread_id=thread_id,
            workspace_id=ws_id,
            lead_id=lead_db_id,
        )

    # 8. Classify
    try:
        agent_result = await classify_and_draft_reply(
            inbound_reply=payload.reply_body,
            calendar_link=calendar_link,
            workspace_id=ws_id,
        )
    except Exception as exc:
        log.warning(
            "webhook_classify_failed",
            extra={"email": email, "workspace_id": ws_id, "error": str(exc)},
        )
        return WebhookResult(
            ok=False,
            status="failed",
            workspace_id=ws_id,
            lead_id=lead_db_id,
            detail=f"Classification failed: {type(exc).__name__}: {exc}",
        )

    auto_status = _classification_to_status(
        agent_result.classification, agent_result.recommended_action
    )

    # 9. Store classified thread
    thread_id: int | None = None
    try:
        with session_scope() as session:
            th = ReplyThread(
                workspace_id=ws_id,
                lead_id=lead_db_id,
                campaign_id=campaign_id,
                instantly_lead_id=payload.lead_id or None,
                prospect_email=email,
                prospect_name=prospect_name,
                company_name=payload.company,
                thread_id=payload.thread_id,
                inbound_reply_text=payload.reply_body,
                classification=agent_result.classification,
                recommended_action=agent_result.recommended_action,
                draft_body=agent_result.draft_body,
                human_review_notes=agent_result.human_review_notes,
                status=auto_status,
                raw_payload=raw_payload,
                dedup_key=dedup_key,
            )
            session.add(th)
            session.flush()
            thread_id = th.id
    except IntegrityError:
        thread_id = _fetch_thread_id(ws_id, campaign_id, dedup_key)
        return WebhookResult(
            ok=True,
            status="duplicate",
            reply_thread_id=thread_id,
            workspace_id=ws_id,
            lead_id=lead_db_id,
            detail="Duplicate — race condition on insert.",
        )

    log.info(
        "webhook_reply_stored",
        extra={
            "thread_id": thread_id,
            "workspace_id": ws_id,
            "email": email,
            "status": auto_status,
            "classification": agent_result.classification,
        },
    )

    return WebhookResult(
        ok=True,
        status=auto_status,
        reply_thread_id=thread_id,
        workspace_id=ws_id,
        lead_id=lead_db_id,
        classification=agent_result.classification,
    )


def _fetch_thread_id(ws_id: int, campaign_id: str, dedup_key: str) -> int | None:
    """Return the id of an existing thread by dedup key, or None."""
    with session_scope() as session:
        row = session.execute(
            select(ReplyThread).where(
                ReplyThread.workspace_id == ws_id,
                ReplyThread.campaign_id == campaign_id,
                ReplyThread.dedup_key == dedup_key,
            )
        ).scalar_one_or_none()
        return row.id if row else None
