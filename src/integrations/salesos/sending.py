"""SalesOS approval-gated sending.

Finds outbound content a CSM has APPROVED in SalesOS but not yet sent, applies
any CSM edits, re-runs ALL safety checks, and delivers through Instantly via the
existing delivery primitive (``deliver_email``) — which itself re-enforces the
full guard order including the approval gate. Delivery outcomes are written back
to ``salesos_delivery_events`` and reflected on ``salesos_outbound_content``.

Hard rules (integration mode):
  - A lead can only send when content exists, content safety passes, email is
    verified, no duplicate send exists, tier meets threshold, AND an approval row
    has approval_status='approved'. Missing approval blocks with
    ``missing_salesos_csm_approval`` and the lead is NEVER marked sent.
  - Approval edits never inject internal/placeholder text past the safety gate —
    the content-safety check re-runs on the edited copy before any send.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from src.db import session_scope
from src.integrations.salesos import ensure_salesos_tables
from src.integrations.salesos.models import (
    APPROVAL_APPROVED,
    CONTENT_REJECTED,
    CONTENT_SENT,
    DeliveryEvent,
    OutboundApproval,
    OutboundContent,
    SalesOSLead,
)
from src.models import GeneratedContent, Lead, now_utc

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discovery: approved but not yet sent
# ---------------------------------------------------------------------------

def find_approved_unsent(
    *, limit: int = 10, workspace_id: int | None = None, client_id: str | None = None
) -> list[dict]:
    """Return approved-but-unsent content rows (oldest approval first).

    Joins approvals → content → SalesOS lead so results can be tenant-scoped.
    Each item carries the engine link (engine_content_id) and the CSM edits.
    """
    ensure_salesos_tables()
    with session_scope() as session:
        stmt = (
            select(OutboundContent, OutboundApproval, SalesOSLead)
            .join(OutboundApproval, OutboundApproval.content_id == OutboundContent.id)
            .join(SalesOSLead, SalesOSLead.id == OutboundContent.lead_id)
            .where(
                OutboundApproval.approval_status == APPROVAL_APPROVED,
                OutboundContent.content_status != CONTENT_SENT,
                OutboundContent.content_status != CONTENT_REJECTED,
            )
            .order_by(OutboundApproval.approved_at.asc().nullslast())
            .limit(limit)
        )
        if workspace_id is not None:
            stmt = stmt.where(SalesOSLead.workspace_id == workspace_id)
        if client_id is not None:
            stmt = stmt.where(SalesOSLead.client_id == client_id)

        rows = session.execute(stmt).all()
        out: list[dict] = []
        for content, approval, lead in rows:
            out.append({
                "content_id": content.id,
                "salesos_lead_id": lead.id,
                "engine_content_id": content.engine_content_id,
                "approval_id": approval.id,
                "edited_subject": approval.edited_subject,
                "edited_body": approval.edited_body,
            })
        return out


# ---------------------------------------------------------------------------
# Edits + bookkeeping
# ---------------------------------------------------------------------------

def _apply_csm_edits(engine_content_id: int, edited_subject, edited_body) -> int | None:
    """Apply CSM edits to the engine content row; return its engine lead id.

    Only non-empty edits overwrite the generated copy. Returns the engine
    lead_id so the caller can run the eligibility filter + delivery.
    """
    with session_scope() as session:
        content = session.get(GeneratedContent, engine_content_id)
        if content is None:
            return None
        if edited_subject:
            content.subject = edited_subject
        if edited_body:
            content.body = edited_body
        return content.lead_id


def _record_event(
    *, salesos_lead_id: str, content_id: str, status: str,
    instantly_lead_id: str | None = None, campaign_id: str | None = None,
    error: str | None = None,
) -> None:
    with session_scope() as session:
        session.add(DeliveryEvent(
            lead_id=salesos_lead_id,
            content_id=content_id,
            destination="instantly",
            status=status,
            sent_at=now_utc() if status == "sent" else None,
            instantly_lead_id=instantly_lead_id,
            campaign_id=campaign_id,
            error=(error[:4000] if error else None),
        ))
        content = session.get(OutboundContent, content_id)
        if content is not None:
            if status == "sent":
                content.content_status = CONTENT_SENT
                content.blocked_reason = None
            else:
                content.blocked_reason = error


def _workspace_for_engine_lead(engine_lead_id: int) -> int | None:
    with session_scope() as session:
        lead = session.get(Lead, engine_lead_id)
        return lead.workspace_id if lead is not None else None


# ---------------------------------------------------------------------------
# Send one approved item
# ---------------------------------------------------------------------------

def send_one(item: dict, *, dry_run: bool = False) -> dict:
    """Send one approved content item. Never raises — outcome is captured."""
    from src.delivery.eligibility import filter_eligible

    content_id = item["content_id"]
    salesos_lead_id = item["salesos_lead_id"]
    engine_content_id = item.get("engine_content_id")

    if engine_content_id is None:
        _record_event(salesos_lead_id=salesos_lead_id, content_id=content_id,
                      status="failed", error="missing_engine_content_link")
        return {"content_id": content_id, "status": "failed",
                "error": "missing_engine_content_link"}

    engine_lead_id = _apply_csm_edits(
        engine_content_id, item.get("edited_subject"), item.get("edited_body")
    )
    if engine_lead_id is None:
        _record_event(salesos_lead_id=salesos_lead_id, content_id=content_id,
                      status="failed", error="engine_content_not_found")
        return {"content_id": content_id, "status": "failed",
                "error": "engine_content_not_found"}

    # Pre-flight: the shared eligibility gate enforces every safety check AND the
    # approval gate (integration mode). Gives a clean blocked_reason for reporting.
    with session_scope() as session:
        eligible, skipped = filter_eligible([engine_lead_id], session)
    if engine_lead_id not in eligible:
        reason = next((code for code, ids in skipped.items() if engine_lead_id in ids), "blocked")
        _record_event(salesos_lead_id=salesos_lead_id, content_id=content_id,
                      status="blocked", error=reason)
        log.info("salesos_send_blocked",
                 extra={"content_id": content_id, "reason": reason})
        return {"content_id": content_id, "status": "blocked", "error": reason}

    if dry_run:
        return {"content_id": content_id, "status": "would_send",
                "engine_lead_id": engine_lead_id}

    workspace_id = _workspace_for_engine_lead(engine_lead_id)
    try:
        from src.delivery.instantly import deliver_email
        result = asyncio.run(deliver_email(engine_lead_id, workspace_id=workspace_id))
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        _record_event(salesos_lead_id=salesos_lead_id, content_id=content_id,
                      status="failed", error=err)
        return {"content_id": content_id, "status": "failed", "error": err}

    if result.delivered:
        _record_event(salesos_lead_id=salesos_lead_id, content_id=content_id,
                      status="sent", instantly_lead_id=result.delivery_id)
        log.info("salesos_send_complete",
                 extra={"content_id": content_id, "delivery_id": result.delivery_id})
        return {"content_id": content_id, "status": "sent",
                "instantly_lead_id": result.delivery_id}

    # deliver_email re-enforced a guard — block, do NOT mark sent.
    reason = result.skip_reason or "blocked"
    _record_event(salesos_lead_id=salesos_lead_id, content_id=content_id,
                  status="blocked", error=reason)
    return {"content_id": content_id, "status": "blocked", "error": reason}


def send_approved_once(
    *, limit: int = 10, workspace_id: int | None = None,
    client_id: str | None = None, dry_run: bool = False,
) -> dict:
    """Find approved-but-unsent content and send each. Returns a summary."""
    items = find_approved_unsent(limit=limit, workspace_id=workspace_id, client_id=client_id)
    results = [send_one(it, dry_run=dry_run) for it in items]
    sent = sum(1 for r in results if r["status"] == "sent")
    blocked = sum(1 for r in results if r["status"] == "blocked")
    return {"found": len(items), "sent": sent, "blocked": blocked,
            "results": results, "dry_run": dry_run}
