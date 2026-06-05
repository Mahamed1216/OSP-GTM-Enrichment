"""Reply queue: sync opportunity replies from Instantly, classify, auto-draft.

Hard constraints (never relaxed):
- Never auto-sends a reply.
- Never modifies campaigns or email copy.
- All send attempts require explicit operator approval.
- If Instantly reply-send endpoint unavailable → manual_send_required.
- No placeholder records in needs_review — missing reply body → missing_reply_text.

Opportunity filter: only leads with Instantly status >= 3 (Interested / Opportunity /
Booked / Converted) or explicit opportunity/interest flags are processed.
Status 2 (generic replied) is intentionally excluded because it includes OOO,
unsubscribe, and negative replies — it would flood the queue.
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
from src.feedback.engagement import _iter_campaign_leads
from src.feedback.reply_agent import (
    ACTION_HUMAN,
    ACTION_STOP,
    INTENT_ANGRY,
    INTENT_UNSUBSCRIBE,
    classify_and_draft_reply,
)
from src.models import Engagement, GeneratedContent, Lead, ReplyThread
from src.retry import retry_api

log = logging.getLogger(__name__)

_API_BASE = "https://api.instantly.ai/api/v2"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_APPROVED = "approved"
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_NEEDS_HUMAN = "needs_human_review"
STATUS_MANUAL_SEND = "manual_send_required"
STATUS_MISSING_TEXT = "missing_reply_text"   # opportunity/replied lead, body unavailable

_SEND_BLOCKED_CLASSIFICATIONS = frozenset({INTENT_UNSUBSCRIBE, INTENT_ANGRY})
_SEND_BLOCKED_ACTIONS = frozenset({ACTION_STOP, ACTION_HUMAN})

_PLACEHOLDER_PREFIX = "[Reply text not available"

# Classifications that warrant a needs_review draft
POSITIVE_CLASSIFICATIONS = frozenset({
    "positive_interest",
    "meeting_request",
    "commercial_terms_shared",
    "revshare_or_bounty_offer",
    "pricing_question",
})

# ---------------------------------------------------------------------------
# Opportunity filter
# ---------------------------------------------------------------------------
_OPPORTUNITY_STATUS_INTS: frozenset[int] = frozenset({3, 4, 5, 6})

_OPPORTUNITY_STATUS_STRS: frozenset[str] = frozenset({
    "interested", "meeting_booked", "opportunity", "customer",
    "booked", "converted", "won", "qualified", "hot",
})

_OPPORTUNITY_SIGNAL_FIELDS_INT = (
    "status", "lead_status", "email_status", "interest_status",
    "reply_status", "campaign_status",
)
_OPPORTUNITY_BOOL_FLAGS = (
    "is_interested", "has_opportunity", "opportunity",
    "is_opportunity", "interested", "is_booked",
)
_OPPORTUNITY_CATEGORY_FIELDS = ("categories", "labels", "tags")


def _truthy(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "null")
    return True


def _is_opportunity_lead(raw: dict) -> str | None:
    """Return 'field=value' if this lead is a confirmed opportunity, else None.

    status=2 (generic replied) is EXCLUDED — it includes OOO, unsubscribe,
    and negative replies, causing the 309-record flood.
    Only status >= 3 (Interested/Opportunity/Booked/Converted) passes.
    """
    for field in _OPPORTUNITY_SIGNAL_FIELDS_INT:
        val = raw.get(field)
        if val is None:
            continue
        if isinstance(val, int) and val in _OPPORTUNITY_STATUS_INTS:
            return f"{field}={val}"
        if isinstance(val, str) and val.strip().lower() in _OPPORTUNITY_STATUS_STRS:
            return f"{field}={val.strip()}"
    for field in _OPPORTUNITY_BOOL_FLAGS:
        if _truthy(raw.get(field)):
            return f"{field}=true"
    for field in _OPPORTUNITY_CATEGORY_FIELDS:
        cats = raw.get(field)
        if isinstance(cats, list):
            for cat in cats:
                if isinstance(cat, str) and cat.strip().lower() in _OPPORTUNITY_STATUS_STRS:
                    return f"{field}={cat.strip()}"
    return None


# ---------------------------------------------------------------------------
# Schema safety
# ---------------------------------------------------------------------------

def ensure_reply_agent_schema() -> bool:
    """Ensure reply_drafts and reply_threads tables exist. Idempotent."""
    from src.db import ensure_tables
    return ensure_tables("reply_drafts", "reply_threads")


# ---------------------------------------------------------------------------
# Auth / dedup helpers
# ---------------------------------------------------------------------------

def _auth_headers(api_key: str | None = None) -> dict[str, str]:
    key = api_key or settings.instantly_api_key or ""
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _make_dedup_key(
    message_id: str | None,
    instantly_lead_id: str | None,
    prospect_email: str,
    reply_text: str = "",
) -> str:
    if message_id:
        return f"msg:{message_id}"
    if instantly_lead_id:
        return f"lead:{instantly_lead_id}"
    raw = f"{prospect_email.lower().strip()}:{reply_text[:200].strip()}"
    return f"hash:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


# ---------------------------------------------------------------------------
# Reply text fetch — multi-endpoint with full debug
# ---------------------------------------------------------------------------

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
    """Try GET /api/v2/emails. Returns (email_records, debug)."""
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


@retry_api
async def _get_lead_detail_raw(
    instantly_lead_id: str,
    api_key: str | None = None,
) -> dict:
    """GET /api/v2/leads/{id} — full lead detail including any reply fields."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/leads/{instantly_lead_id}",
            headers=_auth_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


def _extract_reply_from_lead_data(lead_data: dict) -> str:
    """Scan a lead's full API response for any inbound reply body text.

    Checks multiple possible field names that Instantly may use across API versions.
    Returns the first non-empty string found, or "" if nothing found.
    Debug: all fields with "reply" in their name are logged by the caller.
    """
    # Common direct reply body fields
    for field in (
        "last_reply_body", "reply_body", "email_reply_body",
        "latest_reply", "reply_text", "last_reply",
        "inbound_reply", "reply_message", "last_message_body",
        "latest_message", "reply_content",
    ):
        val = lead_data.get(field)
        if val and isinstance(val, str) and val.strip():
            return val.strip()

    # Nested conversation / message arrays (check most recent item first)
    for field in ("conversation", "messages", "email_history", "thread", "activities"):
        items = lead_data.get(field)
        if not isinstance(items, list) or not items:
            continue
        # Try inbound/reply-typed items first
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            itype = str(item.get("type") or item.get("direction") or item.get("kind") or "").lower()
            if itype in ("reply", "inbound", "received", "incoming"):
                text = _extract_email_text(item)
                if text:
                    return text
        # Fallback: most recent item regardless of type
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            text = _extract_email_text(item)
            if text:
                return text

    return ""


async def _fetch_reply_text_for_lead(
    instantly_lead_id: str | None,
    prospect_email: str,
    campaign_id: str,
    api_key: str | None = None,
) -> tuple[str, str, dict]:
    """Try multiple endpoints to retrieve the inbound reply body for a lead.

    Returns (reply_text, source_label, debug_info).

    source_label values:
      "emails_api"  — body found via GET /api/v2/emails
      "lead_api"    — body found via GET /api/v2/leads/{id}
      "unavailable" — neither endpoint returned reply body

    debug_info includes:
      - which endpoints were tried
      - HTTP status for each
      - all top-level fields returned (for diagnosis)
      - any reply-related fields found
    """
    debug: dict[str, Any] = {
        "emails_endpoint": None,
        "lead_endpoint": None,
        "source_used": "unavailable",
        "reply_fields_on_lead": [],
        "all_lead_fields": [],
    }

    # --- Strategy 1: GET /api/v2/emails ---
    emails, email_debug = await _fetch_emails_for_lead(prospect_email, campaign_id, api_key)
    debug["emails_endpoint"] = email_debug

    if emails:
        def _ts(e: dict) -> float:
            t = _extract_email_timestamp(e)
            return t.timestamp() if t else 0.0
        emails.sort(key=_ts, reverse=True)
        text = _extract_email_text(emails[0])
        if text:
            debug["source_used"] = "emails_api"
            return text, "emails_api", debug

    # --- Strategy 2: GET /api/v2/leads/{id} ---
    if instantly_lead_id:
        lead_debug: dict[str, Any] = {
            "endpoint": f"{_API_BASE}/leads/{instantly_lead_id}",
            "status": None,
            "error": None,
        }
        try:
            lead_data = await _get_lead_detail_raw(instantly_lead_id, api_key)
            lead_debug["status"] = 200
            lead_debug["all_fields"] = list(lead_data.keys())[:40]
            debug["all_lead_fields"] = lead_debug["all_fields"]

            # Surface all reply-related fields for debug visibility
            reply_fields = {
                k: (str(v)[:120] if v else None)
                for k, v in lead_data.items()
                if any(kw in k.lower() for kw in ("reply", "message", "conversation", "thread", "inbox"))
            }
            lead_debug["reply_fields_found"] = reply_fields
            debug["reply_fields_on_lead"] = list(reply_fields.keys())

            text = _extract_reply_from_lead_data(lead_data)
            if text:
                debug["source_used"] = "lead_api"
                debug["lead_endpoint"] = lead_debug
                return text, "lead_api", debug

        except httpx.HTTPStatusError as exc:
            lead_debug["status"] = exc.response.status_code
            lead_debug["error"] = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        except Exception as exc:
            lead_debug["error"] = f"{type(exc).__name__}: {exc}"
        debug["lead_endpoint"] = lead_debug

    debug["source_used"] = "unavailable"
    return "", "unavailable", debug


def _filter_reply_emails(emails: list[dict]) -> list[dict]:
    typed, untyped = [], []
    for e in emails:
        etype = str(
            e.get("type") or e.get("email_type") or e.get("direction") or ""
        ).lower().strip()
        if etype in ("reply", "inbound", "received", "incoming"):
            typed.append(e)
        elif not etype:
            untyped.append(e)
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


# ---------------------------------------------------------------------------
# Classification → status mapping
# ---------------------------------------------------------------------------

def _classification_to_status(classification: str, action: str) -> str:
    """Map reply classification to ReplyThread status.

    Only positive/commercial/meeting classifications produce needs_review.
    Everything else is skipped (non-actionable) or routed to human.
    """
    if classification == INTENT_UNSUBSCRIBE or action == ACTION_STOP:
        return STATUS_SKIPPED
    if classification == INTENT_ANGRY or action == ACTION_HUMAN:
        return STATUS_NEEDS_HUMAN
    if classification in ("out_of_office", "not_interested", "wrong_person", "referral"):
        return STATUS_SKIPPED
    if classification in POSITIVE_CLASSIFICATIONS:
        return STATUS_NEEDS_REVIEW
    # Uncertain / unexpected → human review is safer than needs_review
    return STATUS_NEEDS_HUMAN


# ---------------------------------------------------------------------------
# Local replied leads (secondary source)
# ---------------------------------------------------------------------------

def _get_local_replied_leads(workspace_id: int | None) -> list[dict]:
    """Return leads with replied=True in the local Engagement table.

    These are candidates that replied but may not have been explicitly
    categorized as opportunities in Instantly (status may be 2).
    Each entry has: instantly_lead_id (delivery_id), email, first_name, last_name, company.
    """
    if workspace_id is None:
        return []
    results = []
    with session_scope() as session:
        rows = session.execute(
            select(
                GeneratedContent.delivery_id,
                Lead.email,
                Lead.first_name,
                Lead.last_name,
                Lead.company,
            )
            .join(Engagement, Engagement.content_id == GeneratedContent.id)
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .where(
                Engagement.replied == True,  # noqa: E712
                GeneratedContent.workspace_id == workspace_id,
                GeneratedContent.delivery_id.is_not(None),
            )
            .distinct()
        ).all()
        for row in rows:
            delivery_id, email, first_name, last_name, company = row
            if not email:
                continue
            name = " ".join(filter(None, [first_name, last_name])).strip() or None
            results.append({
                "instantly_lead_id": str(delivery_id) if delivery_id else None,
                "email": email,
                "prospect_name": name,
                "company": company,
                "source": "local_replied",
                "raw_lead": {},
                "opp_signal": "local_engagement.replied=True",
            })
    return results


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

async def sync_reply_queue(
    workspace_id: int | None = None,
    *,
    auto_reply_positive: bool = False,  # future flag — always False for now
) -> dict:
    """Sync opportunity replies from Instantly, classify, and draft responses.

    Two candidate sources:
      1. Instantly campaign leads with _is_opportunity_lead (status >= 3).
      2. Local Engagement records with replied=True (may overlap with #1).

    For each candidate:
      - Try to retrieve reply body via /api/v2/emails then /api/v2/leads/{id}.
      - If no body found → status = missing_reply_text (NOT needs_review).
      - If body found → classify via reply_agent.
        - Positive/commercial/meeting → needs_review (draft created).
        - Angry/sensitive → needs_human_review.
        - OOO/unsubscribe/negative → skipped.

    Returns a summary dict. Never sends anything automatically.
    auto_reply_positive is reserved for future use and has no effect when False.
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
        raise RuntimeError("No campaign ID configured for this workspace — cannot sync reply queue.")
    if not api_key:
        raise RuntimeError("No Instantly API key configured — cannot sync reply queue.")

    _ws_id = workspace_id if workspace_id is not None else get_default_workspace_id()

    # --- Phase 1: Instantly opportunity leads (status >= 3) ---
    total_leads_scanned = 0
    ignored_not_opportunity = 0
    candidates: dict[str, dict] = {}  # keyed by email (lowercase) for dedup

    debug_skipped_signals: list[dict] = []

    async for raw_lead in _iter_campaign_leads(campaign_id, api_key):
        total_leads_scanned += 1
        opp_signal = _is_opportunity_lead(raw_lead)
        if opp_signal is None:
            ignored_not_opportunity += 1
            if len(debug_skipped_signals) < 20:
                debug_skipped_signals.append({
                    "email": raw_lead.get("email", "?"),
                    "status": raw_lead.get("status"),
                    "email_reply_count": raw_lead.get("email_reply_count"),
                    "reason": "no_opportunity_signal",
                })
            continue

        email_key = str(raw_lead.get("email") or "").lower().strip()
        if not email_key:
            continue
        instantly_id = str(raw_lead.get("id") or raw_lead.get("lead_id") or "").strip()
        name = " ".join(filter(None, [raw_lead.get("first_name"), raw_lead.get("last_name")])).strip() or None
        company = str(raw_lead.get("company_name") or raw_lead.get("company") or "").strip() or None
        candidates[email_key] = {
            "instantly_lead_id": instantly_id or None,
            "email": email_key,
            "prospect_name": name,
            "company": company,
            "source": "instantly_opportunity",
            "raw_lead": raw_lead,
            "opp_signal": opp_signal,
        }

    # --- Phase 2: Local replied leads (supplement Instantly opportunities) ---
    local_replied = _get_local_replied_leads(_ws_id)
    local_added = 0
    for lr in local_replied:
        key = lr["email"].lower().strip()
        if key not in candidates:
            candidates[key] = lr
            local_added += 1

    # --- Process each candidate ---
    new_count = 0
    updated_count = 0
    missing_text_count = 0
    needs_review_count = 0
    needs_human_count = 0
    skipped_count = 0
    error_count = 0
    debug_text_sources: list[dict] = []

    for email_key, cand in candidates.items():
        instantly_id = cand.get("instantly_lead_id")
        prospect_email = cand["email"]
        prospect_name = cand.get("prospect_name")
        company = cand.get("company")
        raw_lead = cand.get("raw_lead") or {}
        opp_signal = cand.get("opp_signal", "unknown")

        # Dedup check
        dedup_key = _make_dedup_key(None, instantly_id, prospect_email)

        with session_scope() as session:
            existing = session.execute(
                select(ReplyThread).where(
                    ReplyThread.workspace_id == _ws_id,
                    ReplyThread.campaign_id == campaign_id,
                    ReplyThread.dedup_key == dedup_key,
                )
            ).scalar_one_or_none()

            if existing is not None:
                existing.raw_payload = raw_lead or existing.raw_payload
                updated_count += 1
                continue

        # Fetch reply body
        reply_text, text_source, text_debug = await _fetch_reply_text_for_lead(
            instantly_id, prospect_email, campaign_id, api_key
        )
        text_debug["prospect_email"] = prospect_email
        text_debug["opp_signal"] = opp_signal
        text_debug["source"] = cand.get("source")
        debug_text_sources.append(text_debug)

        has_real_text = bool(reply_text.strip())

        # Create the record
        new_row_id: int | None = None
        try:
            with session_scope() as session:
                th = ReplyThread(
                    workspace_id=_ws_id,
                    campaign_id=campaign_id,
                    instantly_lead_id=instantly_id or None,
                    prospect_email=prospect_email,
                    prospect_name=prospect_name,
                    company_name=company,
                    inbound_reply_text=(
                        reply_text if has_real_text
                        else f"[Reply text not available — {opp_signal}. Check Instantly dashboard.]"
                    ),
                    status=(STATUS_MISSING_TEXT if not has_real_text else STATUS_NEEDS_REVIEW),
                    raw_payload=raw_lead or None,
                    dedup_key=dedup_key,
                )
                session.add(th)
                session.flush()
                new_row_id = th.id
        except IntegrityError:
            updated_count += 1
            continue
        except Exception as exc:
            log.warning("reply_thread_create_failed", extra={"email": prospect_email, "error": str(exc)})
            error_count += 1
            continue

        if not has_real_text:
            missing_text_count += 1
            with session_scope() as session:
                row = session.get(ReplyThread, new_row_id)
                if row:
                    row.human_review_notes = (
                        f"Reply body not available from Instantly API "
                        f"(tried /api/v2/emails and /api/v2/leads/{{id}}). "
                        f"Opportunity signal: {opp_signal}. "
                        f"Check Instantly dashboard for the actual reply, "
                        f"then use the Manual Draft Tester."
                    )
            new_count += 1
            continue

        # Classify reply
        try:
            agent_result = await classify_and_draft_reply(
                inbound_reply=reply_text,
                calendar_link=calendar_link,
                workspace_id=_ws_id,
            )
            auto_status = _classification_to_status(
                agent_result.classification, agent_result.recommended_action
            )

            with session_scope() as session:
                row = session.get(ReplyThread, new_row_id)
                if row:
                    row.classification = agent_result.classification
                    row.recommended_action = agent_result.recommended_action
                    row.draft_body = agent_result.draft_body
                    row.human_review_notes = agent_result.human_review_notes
                    row.status = auto_status

            if auto_status == STATUS_NEEDS_REVIEW:
                needs_review_count += 1
            elif auto_status == STATUS_NEEDS_HUMAN:
                needs_human_count += 1
            else:
                skipped_count += 1

        except Exception as exc:
            log.warning("reply_auto_draft_failed", extra={"thread_id": new_row_id, "error": str(exc)})

        new_count += 1

    log.info(
        "reply_queue_sync_complete",
        extra={
            "workspace_id": _ws_id,
            "total_scanned": total_leads_scanned,
            "opportunity_leads": len(candidates) - local_added,
            "local_added": local_added,
            "ignored_not_opportunity": ignored_not_opportunity,
            "new": new_count,
            "updated": updated_count,
            "needs_review": needs_review_count,
            "missing_text": missing_text_count,
        },
    )
    return {
        "total_leads_scanned": total_leads_scanned,
        "opportunity_leads_found": len(candidates) - local_added,
        "local_replied_added": local_added,
        "ignored_not_opportunity": ignored_not_opportunity,
        "synced": len(candidates),
        "new": new_count,
        "updated": updated_count,
        "needs_review": needs_review_count,
        "needs_human": needs_human_count,
        "missing_text": missing_text_count,
        "skipped": skipped_count,
        "errors": error_count,
        "api_key_source": api_key_source,
        "debug_text_sources": debug_text_sources,
        "debug_skipped_signals": debug_skipped_signals,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def archive_placeholder_threads(
    workspace_id: int | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """Archive ReplyThread records where reply body was a placeholder.

    Archives records where:
      - inbound_reply_text starts with '[Reply text not available'
      - OR status is already missing_reply_text

    Sent records are always preserved.

    Returns {archived, preserved_real, sent_preserved, dry_run}.
    """
    from src.workspace import get_default_workspace_id
    _ws_id = workspace_id if workspace_id is not None else get_default_workspace_id()

    archived = 0
    preserved_real = 0
    sent_preserved = 0

    with session_scope() as session:
        rows = session.execute(
            select(ReplyThread).where(ReplyThread.workspace_id == _ws_id)
        ).scalars().all()

        for row in rows:
            if row.status == STATUS_SENT:
                sent_preserved += 1
                continue
            is_placeholder = (
                (row.inbound_reply_text or "").startswith(_PLACEHOLDER_PREFIX)
                or row.status == STATUS_MISSING_TEXT
            )
            if is_placeholder:
                archived += 1
                if not dry_run:
                    row.status = STATUS_SKIPPED
                    note = "Archived: placeholder — reply body was not available when this record was created."
                    row.human_review_notes = (note + " " + (row.human_review_notes or "")).strip()
            else:
                preserved_real += 1

    log.info(
        "placeholder_cleanup_complete",
        extra={
            "workspace_id": _ws_id,
            "archived": archived,
            "preserved": preserved_real,
            "dry_run": dry_run,
        },
    )
    return {
        "archived": archived,
        "preserved_real": preserved_real,
        "sent_preserved": sent_preserved,
        "dry_run": dry_run,
    }


def archive_non_opportunity_threads(
    workspace_id: int | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """Archive ReplyThread records that lack a confirmed opportunity signal.

    Checks raw_payload against _is_opportunity_lead. Records with status=sent
    are always preserved.
    """
    from src.workspace import get_default_workspace_id
    _ws_id = workspace_id if workspace_id is not None else get_default_workspace_id()

    archived = 0
    preserved = 0
    no_payload = 0
    sent_preserved = 0

    with session_scope() as session:
        rows = session.execute(
            select(ReplyThread).where(ReplyThread.workspace_id == _ws_id)
        ).scalars().all()

        for row in rows:
            if row.status == STATUS_SENT:
                sent_preserved += 1
                continue
            if not row.raw_payload:
                no_payload += 1
                continue
            signal = _is_opportunity_lead(row.raw_payload)
            if signal is not None:
                preserved += 1
            else:
                archived += 1
                if not dry_run:
                    row.status = STATUS_SKIPPED
                    note = "Archived: not_actual_opportunity — imported when sync filter was too broad."
                    row.human_review_notes = (note + " " + (row.human_review_notes or "")).strip()

    return {
        "archived": archived,
        "preserved": preserved,
        "no_payload": no_payload,
        "sent_preserved": sent_preserved,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Queue read
# ---------------------------------------------------------------------------

def get_reply_queue(
    workspace_id: int | None = None,
    *,
    status_filter: list[str] | None = None,
) -> list[dict]:
    """Return ReplyThreads for the workspace, newest first.

    status_filter: if provided, only return records with matching status.
    """
    from src.workspace import get_default_workspace_id
    _ws_id = workspace_id if workspace_id is not None else get_default_workspace_id()
    with session_scope() as session:
        q = select(ReplyThread).where(ReplyThread.workspace_id == _ws_id)
        if status_filter:
            q = q.where(ReplyThread.status.in_(status_filter))
        rows = session.execute(
            q.order_by(
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


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

async def try_send_reply(
    thread_id: int,
    *,
    workspace_id: int | None = None,
    draft_override: str | None = None,
) -> dict:
    """Attempt to send an approved reply via Instantly /emails/reply.

    Returns {status, detail, debug}.
    404/405/422 → manual_send_required. Never marks as sent on failure.
    """
    from src.workspace import get_api_key_for_workspace, get_campaign_id_for_workspace

    with session_scope() as session:
        row = session.get(ReplyThread, thread_id)
        if row is None:
            return {"status": "failed", "detail": f"Thread {thread_id} not found.", "debug": None}
        thread = _thread_to_dict(row)

    _ws_id = workspace_id if workspace_id is not None else thread["workspace_id"]
    if thread["workspace_id"] is not None and _ws_id != thread["workspace_id"]:
        return {"status": "failed", "detail": "Workspace mismatch.", "debug": None}

    blocked = get_send_blocked_reason(thread)
    if blocked:
        return {"status": "failed", "detail": blocked, "debug": None}

    api_key = get_api_key_for_workspace(_ws_id)
    campaign_id = get_campaign_id_for_workspace(_ws_id) or thread["campaign_id"]

    if not api_key:
        return {"status": "failed", "detail": "No Instantly API key configured.", "debug": None}

    draft_body = (draft_override or thread["draft_body"]).strip()
    if not draft_body:
        return {"status": "failed", "detail": "Draft body is empty.", "debug": None}

    payload: dict = {"campaign": campaign_id, "email": thread["prospect_email"], "body": draft_body}
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
                    f"Instantly threaded reply send is not available (HTTP {resp.status_code}). "
                    "Copy this draft and reply manually through Instantly or your email client."
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
