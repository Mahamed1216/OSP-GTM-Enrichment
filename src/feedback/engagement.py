"""Pull engagement data from Instantly and persist to Engagement table.

Strategy: a single paginated `POST /api/v2/leads/list` per campaign instead
of one `GET /api/v2/leads/{id}` per pushed lead. The bulk call returns the
same `email_*_count` fields per lead, but with two important properties:

  - One HTTP round-trip per ~100 leads vs. N per-lead trips, so transient
    timeouts no longer silently drop half the metrics (the old code marked
    each timed-out lead as "failed" and moved on, which is exactly how
    open/reply numbers went stale on the Engagement page).
  - Fetches EVERY lead in the campaign, not just the rows the local DB
    thinks we pushed. Local DB drift (e.g. a manual push or a row whose
    delivery_id was never persisted) can no longer hide engagement events.
"""
import logging
from datetime import datetime
from typing import Any, AsyncIterator

import httpx
from sqlalchemy import select

from src.config import settings
from src.db import session_scope
from src.models import Engagement, GeneratedContent, InstantlyAnalyticsSnapshot
from src.retry import retry_api

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_API_BASE = "https://api.instantly.ai/api/v2"
_PAGE_SIZE = 100


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.instantly_api_key}",
        "Content-Type": "application/json",
    }


def _truthy(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "null")
    return True


def _parse_metrics(raw: dict) -> dict:
    """Normalize Instantly's per-lead response into our Engagement field set.

    Field map (verified against live `/api/v2/leads/{id}` responses on the
    OSP_GTM_TEST campaign, 296-row sample):

      sent/delivered → `timestamp_last_contact` (alias `timestamp_last_touch`)
        present iff Instantly has fired at least one outbound email. The
        documented `emails_sent_count` field is NOT in the lead payload —
        relying on it leaves every row at sent=False, which is what bit us.
      opened  → `email_open_count > 0`
      clicked → `email_click_count > 0`
      replied → `email_reply_count > 0`
      bounced → status == -1 (Bounced) or any of the legacy bounce fields

    `raw` is preserved verbatim so we can re-derive flags via the backfill
    in scripts/backfill_engagement_flags.py without re-hitting the API.
    """
    status = raw.get("status")
    bounced_status = isinstance(status, int) and status == -1
    # A bounced lead reached the SMTP layer, so Instantly counts it as
    # "sent". Treat any timestamp_last_contact OR bounce as proof of send.
    sent_signal = _truthy(
        raw.get("timestamp_last_contact")
        or raw.get("timestamp_last_touch")
        or raw.get("emails_sent_count")
        or raw.get("sent")
    ) or bounced_status

    return {
        "sent": sent_signal,
        "delivered": sent_signal,
        "opened": _truthy(raw.get("email_open_count") or raw.get("opened")),
        "clicked": _truthy(raw.get("email_click_count") or raw.get("clicked")),
        "replied": _truthy(raw.get("email_reply_count") or raw.get("replied")),
        "bounced": bounced_status
        or _truthy(raw.get("bounced") or raw.get("email_bounce_count")),
        "raw": raw,
    }


def _extract_items(payload: dict) -> list[dict]:
    for key in ("items", "data", "leads"):
        items = payload.get(key)
        if isinstance(items, list):
            return items
    return []


@retry_api
async def _fetch_leads_page(
    client: httpx.AsyncClient, starting_after: str | None
) -> dict:
    body: dict[str, Any] = {
        "campaign": settings.instantly_campaign_id,
        "limit": _PAGE_SIZE,
    }
    if starting_after:
        body["starting_after"] = starting_after
    resp = await client.post(
        f"{_API_BASE}/leads/list",
        headers=_auth_headers(),
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


async def _iter_campaign_leads() -> AsyncIterator[dict]:
    """Yield every lead in the configured campaign, paginated."""
    if not settings.instantly_campaign_id:
        raise RuntimeError(
            "INSTANTLY_CAMPAIGN_ID is not set — cannot sync engagement"
        )

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        starting_after: str | None = None
        while True:
            payload = await _fetch_leads_page(client, starting_after)
            items = _extract_items(payload)
            for item in items:
                yield item
            starting_after = payload.get("next_starting_after")
            if not starting_after or not items:
                return


@retry_api
async def fetch_campaign_analytics(campaign_id: str) -> dict:
    """GET /api/v2/campaigns/analytics — campaign-level Instantly metrics.

    Returns the FIRST analytics record for `campaign_id`. Instantly's API
    returns a list (one entry per requested campaign) under that endpoint;
    we request one campaign at a time and unwrap to a single dict so the
    caller doesn't need to know the array shape.

    Field names below are Instantly's own — never normalise here so the
    debug expander can render the response verbatim.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/campaigns/analytics",
            headers=_auth_headers(),
            params={"campaign_id": campaign_id},
        )
        resp.raise_for_status()
        payload = resp.json()
    # Instantly's analytics endpoint returns a list of per-campaign records.
    if isinstance(payload, list):
        return payload[0] if payload else {}
    if isinstance(payload, dict):
        return payload
    return {}


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_analytics(raw: dict) -> dict:
    """Map Instantly's analytics response to our snapshot columns.

    Instantly v2 keys observed on this endpoint:
      leads_count, contacted_count (sequence started),
      emails_sent_count, open_count, link_click_count,
      reply_count, bounced_count, unsubscribed_count, completed_count,
      unique_opened_count (sometimes absent).
    Any missing key defaults to 0 — never raises.
    """
    return {
        "leads_count": _coerce_int(raw.get("leads_count")),
        "contacted_count": _coerce_int(
            raw.get("contacted_count")
            or raw.get("sequence_started_count")
        ),
        "emails_sent_count": _coerce_int(raw.get("emails_sent_count")),
        "open_count": _coerce_int(raw.get("open_count") or raw.get("opened_count")),
        "unique_open_count": _coerce_optional_int(
            raw.get("unique_opened_count") or raw.get("unique_open_count")
        ),
        "reply_count": _coerce_int(raw.get("reply_count") or raw.get("replied_count")),
        "bounced_count": _coerce_int(
            raw.get("bounced_count")
            or raw.get("bounce_count")
        ),
        "click_count": _coerce_int(
            raw.get("link_click_count")
            or raw.get("click_count")
        ),
        "unsubscribed_count": _coerce_int(raw.get("unsubscribed_count")),
        "completed_count": _coerce_int(raw.get("completed_count")),
    }


async def sync_campaign_analytics() -> dict:
    """Pull campaign-level analytics from Instantly and persist a snapshot.

    Returns a dict with parsed metrics + raw response + the snapshot id
    so the UI can quote a "Last synced from Instantly" timestamp and
    render the raw JSON in the debug expander.
    """
    campaign_id = settings.instantly_campaign_id
    if not settings.instantly_api_key or not campaign_id:
        raise RuntimeError(
            "INSTANTLY_API_KEY or INSTANTLY_CAMPAIGN_ID not set — "
            "cannot sync campaign analytics"
        )
    raw = await fetch_campaign_analytics(campaign_id)
    parsed = _parse_analytics(raw)
    snapshot_id: int
    synced_at = datetime.utcnow()
    with session_scope() as session:
        snapshot = InstantlyAnalyticsSnapshot(
            campaign_id=campaign_id,
            raw=raw or {},
            synced_at=synced_at,
            **parsed,
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id
    log.info(
        "instantly_analytics_synced",
        extra={
            "campaign_id": campaign_id,
            "snapshot_id": snapshot_id,
            **parsed,
        },
    )
    return {
        "snapshot_id": snapshot_id,
        "campaign_id": campaign_id,
        "synced_at": synced_at,
        "raw": raw,
        **parsed,
    }


async def sync_engagement() -> dict:
    """Bulk-pull every lead in the campaign and upsert Engagement rows.

    Builds a {delivery_id -> content_id} map up front so each API lead can
    be matched in O(1). Leads we don't recognise locally are skipped (they
    might be from a manual import) and counted under `unknown`.
    """
    with session_scope() as session:
        rows = session.execute(
            select(GeneratedContent.id, GeneratedContent.delivery_id).where(
                GeneratedContent.delivered_at.is_not(None),
                GeneratedContent.delivery_id.is_not(None),
                GeneratedContent.kind == "email",
            )
        ).all()
    delivery_to_content: dict[str, int] = {
        str(r.delivery_id): int(r.id) for r in rows if r.delivery_id
    }

    synced = 0
    unknown = 0
    failed = 0

    try:
        async for raw in _iter_campaign_leads():
            remote_id = str(raw.get("id") or raw.get("lead_id") or "")
            if not remote_id:
                continue
            content_id = delivery_to_content.get(remote_id)
            if content_id is None:
                unknown += 1
                continue

            metrics = _parse_metrics(raw)
            try:
                with session_scope() as session:
                    existing = session.execute(
                        select(Engagement).where(
                            Engagement.content_id == content_id
                        )
                    ).scalar_one_or_none()
                    if existing:
                        for k, v in metrics.items():
                            setattr(existing, k, v)
                        existing.synced_at = datetime.utcnow()
                    else:
                        session.add(
                            Engagement(content_id=content_id, **metrics)
                        )
                synced += 1
            except Exception as exc:
                log.warning(
                    "engagement_upsert_failed",
                    extra={
                        "content_id": content_id,
                        "remote_id": remote_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                failed += 1
    except Exception as exc:
        log.error(
            "engagement_sync_aborted",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        raise

    total = synced + failed
    log.info(
        "engagement_sync_complete",
        extra={
            "synced": synced,
            "failed": failed,
            "unknown": unknown,
            "total": total,
        },
    )
    return {
        "synced": synced,
        "failed": failed,
        "unknown": unknown,
        "total": total,
    }
