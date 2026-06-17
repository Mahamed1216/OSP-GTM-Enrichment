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
from src.delivery.eligibility import is_unsafe_internal_content
from src.delivery.verify_email import verify_email
from src.models import GeneratedContent, Lead, Score, now_utc
from src.retry import retry_api

log = logging.getLogger(__name__)

SkipReason = Literal[
    "tier_below_threshold",
    "already_delivered",
    "email_invalid",
    "no_email_content",
    "unsafe_internal_review_content",
    "workspace_not_configured",
]


class DeliveryResult(BaseModel):
    delivered: bool
    skip_reason: SkipReason | None = None
    delivery_id: str | None = None
    dry_run: bool = False


_CLIENT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_API_BASE = "https://api.instantly.ai/api/v2"


def _auth_headers(api_key: str | None = None) -> dict[str, str]:
    key = api_key or settings.instantly_api_key or ""
    return {
        "Authorization": f"Bearer {key}",
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
async def get_lead(remote_lead_id: str, api_key: str | None = None) -> dict:
    """GET /api/v2/leads/{id}. Used by the push-one smoke to read back the
    custom_variables that landed on Instantly's side.

    api_key defaults to the env key (single-workspace behavior). Pass a
    workspace-resolved key so multi-workspace flows read from the correct
    Instantly account."""
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/leads/{remote_lead_id}",
            headers=_auth_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


@retry_api
async def _post_lead_to_campaign(payload: dict, api_key: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.post(
            f"{_API_BASE}/leads",
            headers=_auth_headers(api_key),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


@retry_api
async def find_leads_by_email(
    email: str, campaign_id: str, api_key: str | None = None
) -> list[dict]:
    """Look up leads in the configured campaign whose email matches `email`.

    Uses POST /api/v2/leads/list with the campaign filter + search term.
    Instantly's `search` is a substring match across multiple fields, so we
    re-filter the response by exact (case-insensitive) email locally —
    otherwise "alex@x.io" could match "alexandra@x.io" and we'd PATCH the
    wrong record.

    Returns a list (zero, one, or many). The bulk-overwrite caller treats
    >1 as a duplicate-warning case and refuses to update without operator
    intervention.

    api_key defaults to the env key (single-workspace behavior). Pass a
    workspace-resolved key so the lookup runs against the correct campaign's
    Instantly account.
    """
    if not email or not campaign_id:
        return []
    target = email.strip().lower()
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.post(
            f"{_API_BASE}/leads/list",
            headers=_auth_headers(api_key),
            json={"campaign": campaign_id, "search": email, "limit": 50},
        )
        resp.raise_for_status()
        data = resp.json()
    items = data.get("items") or data.get("data") or data.get("leads") or []
    if not isinstance(items, list):
        return []
    return [
        item for item in items
        if isinstance(item, dict)
        and str(item.get("email") or "").strip().lower() == target
    ]


@retry_api
async def patch_lead(
    remote_lead_id: str, payload: dict, api_key: str | None = None
) -> dict:
    """PATCH /api/v2/leads/{id} — update fields on an existing Instantly lead.

    Used by the overwrite flow to update `custom_variables` (subject/body)
    without re-creating the lead or touching the campaign schedule.
    Raises httpx.HTTPStatusError on non-2xx; the caller distinguishes
    "Instantly refused to update" (405/400) from transient failures.

    api_key defaults to the env key (single-workspace behavior). Pass a
    workspace-resolved key so the update targets the correct account.
    """
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.patch(
            f"{_API_BASE}/leads/{remote_lead_id}",
            headers=_auth_headers(api_key),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def _build_payload(lead: Lead, content: GeneratedContent, *, campaign_id: str | None = None) -> dict:
    """Build the Instantly /api/v2/leads payload.

    Body + subject are passed via custom_variables. The campaign sequence
    template MUST reference {{personalized_subject}} and {{personalized_body}}
    — without those placeholders Instantly falls back to its hardcoded
    template content and the generated copy never reaches the recipient.

    Top-level field is `campaign` (not `campaign_id`) per Instantly v2 API.
    Phase 6: campaign_id param overrides the env default.
    """
    return {
        "campaign": campaign_id or settings.instantly_campaign_id,
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
    workspace_id: int | None = None,
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
        content_subject = content.subject if content else None
        content_body = content.body if content else None
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

    # Guard 3b: content safety — never send internal review / placeholder text
    # (e.g. the "NEEDS REVIEW: ..." buyer-research marker). Runs before the
    # verification API call and before any send.
    if is_unsafe_internal_content(content_subject, content_body):
        return await _record_skip(content_id, "unsafe_internal_review_content", lead_id, tier_snapshot)

    # Guard 4: email verification
    verify = await verify_email(lead_id)
    if not _accept_verification(verify.status, strict=strict_verification):
        return await _record_skip(content_id, "email_invalid", lead_id, tier_snapshot, verify_status=verify.status)

    # All guards passed — build payload + send.
    # Phase 6: resolve campaign_id and api_key from workspace.
    # When an explicit workspace is selected, never fall back to the env/default
    # account — a missing workspace config must skip, not send through the wrong
    # campaign. workspace_id=None keeps the single-workspace env fallback.
    from src.workspace import get_campaign_id_for_workspace, get_api_key_for_workspace
    _allow_env_fallback = workspace_id is None
    _campaign_id = get_campaign_id_for_workspace(workspace_id, allow_env_fallback=_allow_env_fallback)
    _api_key = get_api_key_for_workspace(workspace_id, allow_env_fallback=_allow_env_fallback)
    if workspace_id is not None and (not _campaign_id or not _api_key):
        return await _record_skip(content_id, "workspace_not_configured", lead_id, tier_snapshot)
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        content = session.get(GeneratedContent, content_id)
        payload = _build_payload(lead, content, campaign_id=_campaign_id)

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
        response = await _post_lead_to_campaign(payload, api_key=_api_key)
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
        content.delivered_at = now_utc()
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


# ---------------------------------------------------------------------------
# Bulk overwrite flow (Phase: post-prompt-wipe rescue)
# ---------------------------------------------------------------------------

OverwriteStatus = Literal[
    "updated",          # PATCH succeeded and read-back matched
    "created",          # lead wasn't in Instantly — created fresh
    "skipped",          # local guard (no email/no content) — nothing tried
    "failed",           # API error (5xx, network, read-back mismatch)
    "duplicate_warning",  # >1 lead in Instantly with this email — refused to act
    "update_unsupported",  # Instantly rejected PATCH (405/400) — operator must delete+reimport
]


class OverwriteResult(BaseModel):
    lead_id: int
    status: OverwriteStatus
    detail: str | None = None
    remote_lead_id: str | None = None


def _latest_email_content_for(lead_id: int) -> tuple[Lead | None, GeneratedContent | None]:
    """Snapshot the lead row + the most recent email GeneratedContent for it.

    Snapshots happen inside a single session so callers can use the returned
    objects detached. Returns (None, None) if the lead doesn't exist; the
    content half is None when no email has been generated yet.
    """
    from sqlalchemy.orm import make_transient

    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return None, None
        content = session.execute(
            select(GeneratedContent)
            .where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == "email",
            )
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()

        # Expunge so the caller can touch attrs after session close without
        # triggering refresh on the now-detached instances.
        session.expunge(lead)
        if content is not None:
            session.expunge(content)
        return lead, content


def _mark_local_sent(content_id: int, remote_lead_id: str) -> None:
    """Flip the local GeneratedContent row to delivery_status='sent'.

    Used by the overwrite flow after a successful PATCH or POST so the
    Leads-page Status pill flips from Pending → Sent immediately, without
    waiting for the Instantly engagement sync to run.

    - `delivery_status` and `delivery_provider` are always (re-)set to the
      current truth.
    - `delivery_id` is updated to the remote id we just confirmed.
    - `delivered_at` is set ONLY if currently NULL so re-overwriting an
      already-sent lead doesn't backdate the original delivery timestamp.
    - `error_message` is cleared to wipe any stale failure.
    """
    if content_id is None:
        return
    with session_scope() as session:
        content = session.get(GeneratedContent, content_id)
        if content is None:
            return
        if content.delivered_at is None:
            content.delivered_at = now_utc()
        if remote_lead_id:
            content.delivery_id = remote_lead_id
        content.delivery_provider = "instantly"
        content.delivery_status = "sent"
        content.error_message = None


def _read_back_matches(remote: dict, expected_subject: str, expected_body: str) -> bool:
    """Verify Instantly returned the custom_variables we just wrote.

    Empirically, Instantly v2 surfaces custom variables under the top-level
    `payload` key (confirmed by dumping live lead responses on the
    OSP_GTM_TEST campaign — `custom_variables` was never present). We
    accept either key so the check survives any future API change.
    """
    for key in ("payload", "custom_variables"):
        container = remote.get(key)
        if not isinstance(container, dict):
            continue
        if (
            str(container.get("personalized_subject") or "") == (expected_subject or "")
            and str(container.get("personalized_body") or "") == (expected_body or "")
        ):
            return True
    return False


async def overwrite_lead_copy(
    lead_id: int, *, create_if_missing: bool = True, workspace_id: int | None = None
) -> OverwriteResult:
    """Push the latest local email copy to Instantly for one lead.

    Per-lead flow:
      1. Snapshot the lead's email + most-recent GeneratedContent.
      2. Search Instantly's campaign for that email (exact match).
      3. >1 match → duplicate_warning (refuses to act).
      4. 1 match  → PATCH custom_variables, then GET and verify.
      5. 0 match  → POST a new lead (only if create_if_missing).

    Credentials are resolved per workspace (same resolver as
    ``deliver_email``): the workspace's Instantly campaign id + API key, with
    the env var as fallback when the workspace has none configured.
    ``workspace_id=None`` resolves to the default workspace, preserving
    single-workspace behavior. If neither workspace nor env yields both a
    campaign id and an API key, the call fails clearly instead of silently
    acting on the wrong (env/default) account.

    Never deletes, never activates the campaign, never touches
    delivered_at/delivery_status on existing rows (so the engagement
    sync continues to be the source of truth for "actually sent").
    """
    from src.workspace import get_api_key_for_workspace, get_campaign_id_for_workspace

    # When an explicit workspace is selected, never fall back to the env/default
    # account — a missing workspace config must fail, not send through the wrong
    # campaign. workspace_id=None keeps the single-workspace env fallback.
    _allow_env_fallback = workspace_id is None
    campaign_id = get_campaign_id_for_workspace(workspace_id, allow_env_fallback=_allow_env_fallback)
    api_key = get_api_key_for_workspace(workspace_id, allow_env_fallback=_allow_env_fallback)
    if not api_key or not campaign_id:
        return OverwriteResult(
            lead_id=lead_id,
            status="failed",
            detail="Instantly API key or campaign ID not configured for this workspace",
        )

    lead, content = _latest_email_content_for(lead_id)
    if lead is None:
        return OverwriteResult(lead_id=lead_id, status="skipped", detail="lead not found")
    if not lead.email:
        return OverwriteResult(lead_id=lead_id, status="skipped", detail="lead has no email")
    if content is None or not (content.body or "").strip():
        return OverwriteResult(lead_id=lead_id, status="skipped", detail="no email content generated")

    # Hard content-safety gate: never overwrite/push internal review or
    # placeholder text (e.g. the "NEEDS REVIEW: ..." marker). Returns BEFORE
    # any Instantly lookup/PATCH/POST, so existing valid Instantly copy is
    # never clobbered with internal text.
    if is_unsafe_internal_content(content.subject, content.body):
        return OverwriteResult(
            lead_id=lead_id,
            status="skipped",
            detail="unsafe_internal_review_content: content needs review/regeneration",
        )

    expected_subject = content.subject or ""
    expected_body = content.body

    try:
        matches = await find_leads_by_email(lead.email, campaign_id, api_key=api_key)
    except Exception as exc:
        return OverwriteResult(
            lead_id=lead_id, status="failed",
            detail=f"lookup failed: {_format_error(exc)}",
        )

    if len(matches) > 1:
        return OverwriteResult(
            lead_id=lead_id,
            status="duplicate_warning",
            detail=f"{len(matches)} leads in campaign share this email — refusing to update",
        )

    if len(matches) == 1:
        remote_id = str(matches[0].get("id") or matches[0].get("lead_id") or "")
        if not remote_id:
            return OverwriteResult(
                lead_id=lead_id, status="failed",
                detail="matched lead has no id in response",
            )
        patch_payload = {
            "custom_variables": {
                "personalized_subject": expected_subject,
                "personalized_body": expected_body,
                "signals_cited": ", ".join(content.signals_cited or []),
            }
        }
        try:
            await patch_lead(remote_id, patch_payload, api_key=api_key)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (400, 404, 405, 409, 422):
                return OverwriteResult(
                    lead_id=lead_id,
                    status="update_unsupported",
                    detail=(
                        f"Instantly rejected PATCH (HTTP {code}). Delete and "
                        f"reimport this lead manually, or use a delete + recreate flow."
                    ),
                    remote_lead_id=remote_id,
                )
            return OverwriteResult(
                lead_id=lead_id, status="failed",
                detail=_format_error(exc), remote_lead_id=remote_id,
            )
        except Exception as exc:
            return OverwriteResult(
                lead_id=lead_id, status="failed",
                detail=_format_error(exc), remote_lead_id=remote_id,
            )

        try:
            remote = await get_lead(remote_id, api_key=api_key)
        except Exception as exc:
            return OverwriteResult(
                lead_id=lead_id, status="failed",
                detail=f"PATCH succeeded but read-back failed: {_format_error(exc)}",
                remote_lead_id=remote_id,
            )

        if not _read_back_matches(remote, expected_subject, expected_body):
            return OverwriteResult(
                lead_id=lead_id, status="failed",
                detail="read-back custom_variables did not match local content",
                remote_lead_id=remote_id,
            )
        # Local DB → Sent. Preserves original delivered_at if already set.
        _mark_local_sent(content.id, remote_id)
        return OverwriteResult(
            lead_id=lead_id, status="updated",
            detail="custom_variables updated and verified",
            remote_lead_id=remote_id,
        )

    # No match — create only if allowed.
    if not create_if_missing:
        return OverwriteResult(
            lead_id=lead_id, status="skipped",
            detail="not in Instantly campaign; create_if_missing=False",
        )

    payload = _build_payload(lead, content, campaign_id=campaign_id)
    try:
        response = await _post_lead_to_campaign(payload, api_key=api_key)
    except Exception as exc:
        return OverwriteResult(
            lead_id=lead_id, status="failed",
            detail=f"create failed: {_format_error(exc)}",
        )
    new_remote_id = str(response.get("id") or response.get("lead_id") or "")

    try:
        remote = await get_lead(new_remote_id, api_key=api_key) if new_remote_id else {}
    except Exception:
        remote = {}
    verified = _read_back_matches(remote, expected_subject, expected_body) if remote else False
    # POST succeeded → Instantly accepted the lead. Mark local sent now so
    # the Leads-page status flips immediately, even if the read-back failed
    # (which is a verification problem, not a delivery problem).
    _mark_local_sent(content.id, new_remote_id)
    detail = "created" + ("" if verified else " (read-back unverified)")
    return OverwriteResult(
        lead_id=lead_id, status="created",
        detail=detail, remote_lead_id=new_remote_id or None,
    )


# ---------------------------------------------------------------------------
# One-lead diagnostic — temporary, for proving what Instantly actually does
# ---------------------------------------------------------------------------

async def _raw_request(
    method: str, url: str, *, json_body: dict | None = None, api_key: str | None = None,
) -> dict:
    """Issue a raw httpx call (no retries) and capture the full response.

    Used by the debug flow so failures, error bodies, and unexpected shapes
    surface verbatim instead of being eaten by @retry_api. Returns a dict
    with request_url, request_body, status, response_json, response_text.

    api_key defaults to the env key; pass a workspace-resolved key so the
    diagnostic hits the same account the real overwrite flow would use.
    """
    safe_headers = {"Authorization": "Bearer ***REDACTED***", "Content-Type": "application/json"}
    record: dict = {
        "method": method,
        "request_url": url,
        "request_headers": safe_headers,
        "request_body": json_body,
        "status": None,
        "response_json": None,
        "response_text": None,
        "error": None,
    }
    try:
        async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
            resp = await client.request(
                method, url, headers=_auth_headers(api_key), json=json_body,
            )
        record["status"] = resp.status_code
        text = resp.text
        record["response_text"] = text
        try:
            record["response_json"] = resp.json()
        except Exception:
            record["response_json"] = None
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


async def debug_overwrite_one_lead(lead_id: int, *, workspace_id: int | None = None) -> dict:
    """Run the overwrite flow for one lead with FULL HTTP capture.

    Does not delete, does not activate the campaign, does not retry. Only
    touches the single lead identified by `lead_id` and its matching
    Instantly record. Captures the 20 fields the operator needs to tell
    apart wrong-id / wrong-endpoint / wrong-body / wrong-key / not-supported.

    Credentials are resolved per workspace (same resolver as the real
    overwrite flow), so the diagnostic reflects the account the selected
    workspace would actually act on. ``workspace_id=None`` resolves to the
    default workspace.

    Always returns a dict (never raises). Caller renders it in the UI.
    """
    from src.workspace import get_api_key_for_workspace, get_campaign_id_for_workspace

    # Explicit workspace → no env fallback (see overwrite_lead_copy).
    _allow_env_fallback = workspace_id is None
    campaign_id = get_campaign_id_for_workspace(workspace_id, allow_env_fallback=_allow_env_fallback)
    api_key = get_api_key_for_workspace(workspace_id, allow_env_fallback=_allow_env_fallback)

    out: dict = {
        "local_lead_id": lead_id,
        "local_email": None,
        "local_latest_subject": None,
        "local_latest_body_preview": None,
        "local_latest_body": None,
        "campaign_id": campaign_id,
        "search": None,
        "search_match_count": 0,
        "selected_remote_lead_id": None,
        "selected_remote_lead_snapshot": None,
        "update": None,
        "readback": None,
        "remote_subject_after": None,
        "remote_body_after": None,
        "comparison": None,
        "verdict": None,
    }

    if not api_key or not campaign_id:
        out["verdict"] = "Instantly API key or campaign ID not configured for this workspace"
        return out

    lead, content = _latest_email_content_for(lead_id)
    if lead is None:
        out["verdict"] = f"lead {lead_id} not found locally"
        return out
    if content is None:
        out["verdict"] = "no email content generated locally for this lead"
        out["local_email"] = lead.email
        return out

    # Content-safety gate: the diagnostic PATCHes Instantly with this content,
    # so it must also refuse internal review / placeholder text before any push.
    if is_unsafe_internal_content(content.subject, content.body):
        out["local_email"] = lead.email
        out["local_latest_subject"] = content.subject
        out["local_latest_body"] = content.body or ""
        out["verdict"] = (
            "unsafe_internal_review_content: content needs review/regeneration — "
            "refusing to push internal text to Instantly."
        )
        return out

    out["local_email"] = lead.email
    out["local_latest_subject"] = content.subject
    body = content.body or ""
    out["local_latest_body"] = body
    out["local_latest_body_preview"] = body[:280]

    # --- 1) Search Instantly for the lead by email + campaign ---
    search_url = f"{_API_BASE}/leads/list"
    search_body = {
        "campaign": campaign_id,
        "search": lead.email,
        "limit": 50,
    }
    out["search"] = await _raw_request("POST", search_url, json_body=search_body, api_key=api_key)
    items: list[dict] = []
    if out["search"] and isinstance(out["search"].get("response_json"), dict):
        payload = out["search"]["response_json"]
        for key in ("items", "data", "leads"):
            v = payload.get(key)
            if isinstance(v, list):
                items = v
                break
    target = (lead.email or "").strip().lower()
    matches = [
        it for it in items
        if isinstance(it, dict)
        and str(it.get("email") or "").strip().lower() == target
    ]
    out["search_match_count"] = len(matches)

    if not matches:
        out["verdict"] = (
            "No matching lead in Instantly campaign — overwrite would create "
            "a new lead. Check campaign_id + email casing."
        )
        return out
    if len(matches) > 1:
        out["verdict"] = (
            f"{len(matches)} matching leads in campaign — refusing to update "
            f"any to avoid touching the wrong record. De-dupe in Instantly."
        )
        return out

    remote = matches[0]
    remote_id = str(remote.get("id") or remote.get("lead_id") or "")
    out["selected_remote_lead_id"] = remote_id
    out["selected_remote_lead_snapshot"] = remote
    if not remote_id:
        out["verdict"] = "matched lead has no id in search response"
        return out

    # --- 2) PATCH the lead's custom variables (same body shape the production
    #         orchestrator uses, so the diagnostic reveals real behavior) ---
    update_url = f"{_API_BASE}/leads/{remote_id}"
    update_body = {
        "custom_variables": {
            "personalized_subject": content.subject or "",
            "personalized_body": body,
            "signals_cited": ", ".join(content.signals_cited or []),
        }
    }
    out["update"] = await _raw_request("PATCH", update_url, json_body=update_body, api_key=api_key)

    # --- 3) Read back the lead and surface what Instantly actually stored ---
    readback_url = f"{_API_BASE}/leads/{remote_id}"
    out["readback"] = await _raw_request("GET", readback_url, api_key=api_key)

    rb_json = (out["readback"] or {}).get("response_json") or {}
    # Custom vars empirically live under `payload`; also check `custom_variables`
    # in case Instantly ever returns them there.
    sub_after = None
    body_after = None
    for container_key in ("payload", "custom_variables"):
        container = rb_json.get(container_key)
        if isinstance(container, dict):
            if sub_after is None and container.get("personalized_subject"):
                sub_after = container.get("personalized_subject")
            if body_after is None and container.get("personalized_body"):
                body_after = container.get("personalized_body")
    out["remote_subject_after"] = sub_after
    out["remote_body_after"] = body_after

    subject_match = (sub_after or "") == (content.subject or "")
    body_match = (body_after or "") == body
    out["comparison"] = {
        "subject_match": subject_match,
        "body_match": body_match,
        "expected_subject": content.subject,
        "expected_body_preview": body[:280],
        "actual_subject": sub_after,
        "actual_body_preview": (body_after or "")[:280],
    }

    update_status = (out["update"] or {}).get("status")
    if subject_match and body_match:
        out["verdict"] = "SUCCESS — Instantly returned the values we wrote."
    elif update_status and 200 <= int(update_status) < 300:
        out["verdict"] = (
            f"PATCH returned HTTP {update_status} but read-back shows the "
            f"values were not updated. Likely cause: request body shape — "
            f"Instantly may require `payload: {{...}}` instead of "
            f"`custom_variables: {{...}}` on PATCH, or this endpoint does "
            f"not support updating these variables at all."
        )
    elif update_status:
        out["verdict"] = (
            f"PATCH failed with HTTP {update_status}. Inspect 'update' "
            f"response_json/response_text for Instantly's error message — "
            f"that determines whether it's a wrong endpoint, wrong body, or "
            f"unsupported operation."
        )
    else:
        out["verdict"] = (
            "PATCH never returned (network/timeout). See 'update.error'."
        )
    return out
