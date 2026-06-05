"""Reply queue: sync opportunity replies from Instantly and auto-draft responses.

MVP constraints (hard):
- Never auto-sends a reply.
- Never modifies campaigns or email copy.
- All send attempts require explicit operator approval from the UI.
- If Instantly reply-send endpoint is not available, status = manual_send_required.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.db import session_scope
from src.feedback.engagement import _classify_positive_lead, _iter_campaign_leads
from src.feedback.reply_agent import (
    ACTION_HUMAN,
    ACTION_STOP,
    INTENT_ANGRY,
    INTENT_UNSUBSCRIBE,
    classify_and_draft_reply,
)
from src.models import ReplyThread
from src.retry import retry_api

log = logging.getLogger(__name__)

_API_BASE = "https://api.instantly.ai/api/v2"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Status constants
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_APPROVED = "approved"  # set when operator approves, before send attempt
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_NEEDS_HUMAN = "needs_human_review"
STATUS_MANUAL_SEND = "manual_send_required"

_SEND_BLOCKED_CLASSIFICATIONS = frozenset({INTENT_UNSUBSCRIBE, INTENT_ANGRY})
_SEND_BLOCKED_ACTIONS = frozenset({ACTION_STOP, ACTION_HUMAN})

_PLACEHOLDER_PREFIX = "[Reply text not available"


def _auth_headers(api_key: str | None = None) -> dict[str, str]:
    key = api_key or settings.instantly_api_key or ""
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _make_dedup_key(
    message_id: str | None,
    instantly_lead_id: str | None,
    prospect_email: str,
    reply_text: str,
) -> str:
    if message_id:
        return f"msg:{message_id}"
    if instantly_lead_id:
        return f"lead:{instantly_lead_id}"
    raw = f"{prospect_email.lower().strip()}:{reply_text[:200].strip()}"
    return f"hash:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


@retry_api
async def _fetch_emails_raw(
    prospect_email: str,
    campaign_id: str,
    api_key: str | None = None,
    limit: int = 50,
) -> dict:
    """GET /api/v2/emails filtered by lead email + campaign."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/emails",
            headers=_auth_headers(api_key),
            params={"email": prospect_email, "campaign": campaign_id, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()


async def _fetch_emails_for_lead(
    prospect_email: str,
    campaign_id: str,
    api_key: str | None = None,
) -> tuple[list[dict], dict]:
    """Try to fetch email thread records for a lead from Instantly.

    Returns (email_records, debug_info).

    debug_info keys:
      endpoint, params, status, record_count, reply_count,
      fields_found (first-record key names), error
    """
    debug: dict[str, Any] = {
        "endpoint": f"{_API_BASE}/emails",
        "params": {"email": prospect_email, "campaign": campaign_id},
        "status": None,
        "record_count": 0,
        "reply_count": 0,
        "fields_found": [],
        "error": None,
    }
    try:
        raw = await _fetch_emails_raw(prospect_email, campaign_id, api_key)
        debug["status"] = 200
        items: list[dict] = []
        for key in ("items", "data", "emails", "messages"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
        if not items and isinstance(raw, list):
            items = raw
        debug["record_count"] = len(items)
        if items:
            debug["fields_found"] = list(items[0].keys())[:20]
        replies = _filter_reply_emails(items)
        debug["reply_count"] = len(replies)
        return replies, debug
    except httpx.HTTPStatusError as exc:
        debug["status"] = exc.response.status_code
        debug["error"] = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        return [], debug
    except Exception as exc:
        debug["status"] = "error"
        debug["error"] = f"{type(exc).__name__}: {exc}"
        return [], debug


def _filter_reply_emails(emails: list[dict]) -> list[dict]:
    """Return inbound/reply-type emails. Falls back to all when type is absent."""
    typed, untyped = [], []
    for e in emails:
        etype = str(
            e.get("type") or e.get("email_type") or e.get("direction") or ""
        ).lower().strip()
        if etype in ("reply", "inbound", "received", "incoming"):
            typed.append(e)
        elif not etype:
            untyped.append(e)
    # If any emails have an explicit type, return only those; otherwise all unknown.
    return typed if typed else untyped


def _extract_email_text(email: dict) -> str:
    for key in ("body", "text", "html", "content", "message", "body_text", "plain_text"):
        val = email.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_email_timestamp(email: dict) -> datetime | None:
    for key in ("timestamp", "created_at", "received_at", "date", "sent_at"):
        val = email.get(key)
        if not val:
            continue
        if isinstance(val, datetime):
            return val
        if isinstance(val, (int, float)):
            try:
                return datetime.utcfromtimestamp(val / 1000 if val > 1e10 else val)
            except (OSError, OverflowError, ValueError):
                pass
        if isinstance(val, str):
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
            ):
                try:
                    return datetime.strptime(val[:26], fmt)
                except ValueError:
                    continue
    return None


async def sync_reply_queue(workspace_id: int | None = None) -> dict:
    """Sync positive/opportunity replies from Instantly and auto-draft responses.

    Steps:
      1. Iterate all leads in the workspace campaign.
      2. Keep only those with a positive status (opportunity/interested).
      3. For each, try GET /api/v2/emails to fetch reply text.
      4. Upsert a ReplyThread record (deduplicated).
      5. Auto-generate a draft for new records with real reply text.

    Returns a summary dict. Never sends anything automatically.
    """
    from src.workspace import (
        get_api_key_for_workspace,
        get_api_key_source,
        get_calendar_link_for_workspace,
        get_campaign_id_for_workspace,
        get_default_workspace_id,
    )

    campaign_id = get_campaign_id_for_workspace(workspace_id) or settings.instantly_campaign_id
    api_key = get_api_key_for_workspace(workspace_id) or settings.instantly_api_key
    api_key_source = get_api_key_source(workspace_id)
    calendar_link = get_calendar_link_for_workspace(workspace_id) or ""

    if not campaign_id:
        raise RuntimeError(
            "No campaign ID configured for this workspace — cannot sync reply queue."
        )
    if not api_key:
        raise RuntimeError(
            "No Instantly API key configured — cannot sync reply queue."
        )

    _ws_id = workspace_id if workspace_id is not None else get_default_workspace_id()

    synced_positive = 0
    new_count = 0
    updated_count = 0
    skipped_no_text = 0
    error_count = 0
    debug_emails: list[dict] = []

    async for raw_lead in _iter_campaign_leads(campaign_id, api_key):
        pos_signal = _classify_positive_lead(raw_lead)
        if pos_signal is None:
            continue

        synced_positive += 1
        instantly_lead_id = str(raw_lead.get("id") or raw_lead.get("lead_id") or "").strip()
        prospect_email = str(raw_lead.get("email") or "").strip()
        if not prospect_email:
            error_count += 1
            continue

        prospect_name = " ".join(
            filter(None, [raw_lead.get("first_name"), raw_lead.get("last_name")])
        ).strip() or None
        company = str(raw_lead.get("company_name") or raw_lead.get("company") or "").strip() or None

        # Fetch reply text from emails endpoint
        reply_emails, email_debug = await _fetch_emails_for_lead(
            prospect_email, campaign_id, api_key
        )
        email_debug["prospect_email"] = prospect_email
        email_debug["pos_signal"] = pos_signal
        debug_emails.append(email_debug)

        # Pick most-recent reply email
        reply_text = ""
        msg_id: str | None = None
        thread_id_remote: str | None = None
        reply_ts: datetime | None = None

        if reply_emails:
            def _ts_key(e: dict) -> float:
                t = _extract_email_timestamp(e)
                return t.timestamp() if t else 0.0
            reply_emails.sort(key=_ts_key, reverse=True)
            best = reply_emails[0]
            reply_text = _extract_email_text(best)
            msg_id = str(best.get("id") or best.get("message_id") or "").strip() or None
            thread_id_remote = str(
                best.get("thread_id") or best.get("conversation_id") or ""
            ).strip() or None
            reply_ts = _extract_email_timestamp(best)

        has_real_text = bool(reply_text.strip())
        if not has_real_text:
            reply_text = (
                f"[Reply text not available from Instantly API — "
                f"positive signal: {pos_signal}. "
                f"Check Instantly dashboard for the actual reply, "
                f"then use the Manual Draft Tester below.]"
            )
            skipped_no_text += 1

        dedup_key = _make_dedup_key(
            msg_id, instantly_lead_id or None, prospect_email, reply_text
        )

        # Check for existing record
        with session_scope() as session:
            existing = session.execute(
                select(ReplyThread).where(
                    ReplyThread.workspace_id == _ws_id,
                    ReplyThread.campaign_id == campaign_id,
                    ReplyThread.dedup_key == dedup_key,
                )
            ).scalar_one_or_none()

            if existing is not None:
                # Refresh raw payload; upgrade placeholder to real text if now available
                if has_real_text and existing.inbound_reply_text.startswith(_PLACEHOLDER_PREFIX):
                    existing.inbound_reply_text = reply_text
                    existing.reply_received_at = reply_ts
                    existing.message_id = msg_id
                    existing.thread_id = thread_id_remote
                existing.raw_payload = raw_lead
                updated_count += 1
                continue  # don't fall through to create

        # Create new record
        new_row_id: int | None = None
        try:
            with session_scope() as session:
                th = ReplyThread(
                    workspace_id=_ws_id,
                    campaign_id=campaign_id,
                    instantly_lead_id=instantly_lead_id or None,
                    prospect_email=prospect_email,
                    prospect_name=prospect_name,
                    company_name=company,
                    thread_id=thread_id_remote,
                    message_id=msg_id,
                    inbound_reply_text=reply_text,
                    reply_received_at=reply_ts,
                    status=STATUS_NEEDS_REVIEW,
                    raw_payload=raw_lead,
                    dedup_key=dedup_key,
                )
                session.add(th)
                session.flush()
                new_row_id = th.id
        except IntegrityError:
            updated_count += 1
            continue
        except Exception as exc:
            log.warning(
                "reply_thread_create_failed",
                extra={"email": prospect_email, "error": str(exc)},
            )
            error_count += 1
            continue

        # Auto-draft
        if has_real_text and new_row_id is not None:
            try:
                agent_result = await classify_and_draft_reply(
                    inbound_reply=reply_text,
                    calendar_link=calendar_link,
                    workspace_id=_ws_id,
                )
                auto_status = STATUS_NEEDS_REVIEW
                if agent_result.classification == INTENT_UNSUBSCRIBE:
                    auto_status = STATUS_NEEDS_HUMAN
                elif agent_result.recommended_action in (ACTION_STOP, ACTION_HUMAN):
                    auto_status = STATUS_NEEDS_HUMAN

                with session_scope() as session:
                    row = session.get(ReplyThread, new_row_id)
                    if row:
                        row.classification = agent_result.classification
                        row.recommended_action = agent_result.recommended_action
                        row.draft_body = agent_result.draft_body
                        row.human_review_notes = agent_result.human_review_notes
                        row.status = auto_status
            except Exception as exc:
                log.warning(
                    "reply_auto_draft_failed",
                    extra={"thread_id": new_row_id, "error": str(exc)},
                )
        elif not has_real_text and new_row_id is not None:
            with session_scope() as session:
                row = session.get(ReplyThread, new_row_id)
                if row:
                    row.status = STATUS_NEEDS_HUMAN
                    row.human_review_notes = (
                        "No reply text retrieved from Instantly API. "
                        "Check the Instantly dashboard and paste the reply into the Manual Draft Tester."
                    )

        new_count += 1

    log.info(
        "reply_queue_sync_complete",
        extra={
            "workspace_id": _ws_id,
            "positive_leads": synced_positive,
            "new": new_count,
            "updated": updated_count,
            "errors": error_count,
        },
    )
    return {
        "synced": synced_positive,
        "new": new_count,
        "updated": updated_count,
        "skipped_no_text": skipped_no_text,
        "errors": error_count,
        "api_key_source": api_key_source,
        "debug_emails_endpoint": debug_emails,
    }


def get_reply_queue(workspace_id: int | None = None) -> list[dict]:
    """Return all ReplyThreads for the workspace, newest first."""
    from src.workspace import get_default_workspace_id
    _ws_id = workspace_id if workspace_id is not None else get_default_workspace_id()
    with session_scope() as session:
        rows = session.execute(
            select(ReplyThread)
            .where(ReplyThread.workspace_id == _ws_id)
            .order_by(
                ReplyThread.reply_received_at.desc().nullslast(),
                ReplyThread.created_at.desc(),
            )
        ).scalars().all()
        return [_thread_to_dict(r) for r in rows]


def _thread_to_dict(row: ReplyThread) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "campaign_id": row.campaign_id,
        "instantly_lead_id": row.instantly_lead_id,
        "prospect_email": row.prospect_email,
        "prospect_name": row.prospect_name,
        "company_name": row.company_name,
        "thread_id": row.thread_id,
        "message_id": row.message_id,
        "inbound_reply_text": row.inbound_reply_text,
        "original_outbound_email": row.original_outbound_email,
        "reply_received_at": row.reply_received_at,
        "classification": row.classification,
        "recommended_action": row.recommended_action,
        "draft_body": row.draft_body,
        "human_review_notes": row.human_review_notes,
        "status": row.status,
        "sent_at": row.sent_at,
        "send_error": row.send_error,
        "dedup_key": row.dedup_key,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def get_send_blocked_reason(thread: dict) -> str | None:
    """Return a human-readable reason this thread can't be sent, or None if OK."""
    cls = thread.get("classification") or ""
    action = thread.get("recommended_action") or ""
    if cls in _SEND_BLOCKED_CLASSIFICATIONS:
        return f"Send blocked: classification is '{cls}' — no automated reply allowed."
    if action in _SEND_BLOCKED_ACTIONS:
        return f"Send blocked: recommended action is '{action}'."
    if not (thread.get("draft_body") or "").strip():
        return "Send blocked: draft is empty."
    if not (thread.get("campaign_id") or "").strip():
        return "Send blocked: campaign ID is missing."
    return None


async def try_send_reply(
    thread_id: int,
    *,
    workspace_id: int | None = None,
    draft_override: str | None = None,
) -> dict:
    """Attempt to send an approved reply via Instantly's /emails/reply endpoint.

    Returns:
      {"status": "sent" | "failed" | "manual_send_required", "detail": str, "debug": dict}

    If the Instantly reply endpoint returns 404/405/422, status = manual_send_required
    and no row is marked sent. The caller should show the copy-paste prompt to the operator.

    Hard guards before any HTTP call:
      - classification must not be unsubscribe or angry_or_complaint
      - recommended_action must not be route_to_human or stop_sequence
      - draft_body must be non-empty
      - campaign_id and api_key must be available
    """
    from src.workspace import get_api_key_for_workspace, get_campaign_id_for_workspace

    with session_scope() as session:
        row = session.get(ReplyThread, thread_id)
        if row is None:
            return {"status": "failed", "detail": f"Thread {thread_id} not found.", "debug": None}
        thread = _thread_to_dict(row)

    # Workspace isolation
    _ws_id = workspace_id if workspace_id is not None else thread["workspace_id"]
    if thread["workspace_id"] is not None and _ws_id != thread["workspace_id"]:
        return {
            "status": "failed",
            "detail": "Workspace mismatch — cannot send from a different workspace.",
            "debug": None,
        }

    blocked = get_send_blocked_reason(thread)
    if blocked:
        return {"status": "failed", "detail": blocked, "debug": None}

    api_key = get_api_key_for_workspace(_ws_id)
    campaign_id = get_campaign_id_for_workspace(_ws_id) or thread["campaign_id"]

    if not api_key:
        return {
            "status": "failed",
            "detail": "No Instantly API key configured for this workspace.",
            "debug": None,
        }

    draft_body = (draft_override or thread["draft_body"]).strip()
    if not draft_body:
        return {"status": "failed", "detail": "Draft body is empty — nothing to send.", "debug": None}

    payload: dict = {
        "campaign": campaign_id,
        "email": thread["prospect_email"],
        "body": draft_body,
    }
    if thread.get("message_id"):
        payload["reply_to_email_id"] = thread["message_id"]
    if thread.get("thread_id"):
        payload["thread_id"] = thread["thread_id"]

    debug: dict = {
        "endpoint": f"{_API_BASE}/emails/reply",
        "payload_keys": list(payload.keys()),
        "status": None,
        "response": None,
        "error": None,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{_API_BASE}/emails/reply",
                headers=_auth_headers(api_key),
                json=payload,
            )
        debug["status"] = resp.status_code
        try:
            debug["response"] = resp.json()
        except Exception:
            debug["response"] = resp.text[:300]

        if resp.status_code in (404, 405, 422):
            _mark_thread(thread_id, STATUS_MANUAL_SEND, send_error=(
                f"Instantly /emails/reply returned HTTP {resp.status_code} — endpoint not supported."
            ))
            return {
                "status": STATUS_MANUAL_SEND,
                "detail": (
                    f"Instantly threaded reply send is not available with the current API path "
                    f"(HTTP {resp.status_code}). Copy this draft and reply manually "
                    "through Instantly or your email client."
                ),
                "debug": debug,
            }

        resp.raise_for_status()

        _mark_thread(thread_id, STATUS_SENT, sent_at=datetime.utcnow(), send_error=None)
        return {"status": STATUS_SENT, "detail": "Reply sent successfully via Instantly.", "debug": debug}

    except httpx.HTTPStatusError as exc:
        err = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        debug["error"] = err
        _mark_thread(thread_id, STATUS_FAILED, send_error=err)
        return {"status": STATUS_FAILED, "detail": err, "debug": debug}

    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        debug["error"] = err
        _mark_thread(thread_id, STATUS_FAILED, send_error=err)
        return {"status": STATUS_FAILED, "detail": err, "debug": debug}


def _mark_thread(
    thread_id: int,
    status: str,
    *,
    send_error: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    with session_scope() as session:
        row = session.get(ReplyThread, thread_id)
        if row:
            row.status = status
            if send_error is not None:
                row.send_error = send_error
            if sent_at is not None:
                row.sent_at = sent_at


def update_reply_thread(
    thread_id: int,
    *,
    status: str | None = None,
    draft_body: str | None = None,
    classification: str | None = None,
    recommended_action: str | None = None,
    human_review_notes: str | None = None,
) -> bool:
    """Update mutable fields on a ReplyThread. Returns True if found."""
    with session_scope() as session:
        row = session.get(ReplyThread, thread_id)
        if row is None:
            return False
        if status is not None:
            row.status = status
        if draft_body is not None:
            row.draft_body = draft_body
        if classification is not None:
            row.classification = classification
        if recommended_action is not None:
            row.recommended_action = recommended_action
        if human_review_notes is not None:
            row.human_review_notes = human_review_notes
        return True
