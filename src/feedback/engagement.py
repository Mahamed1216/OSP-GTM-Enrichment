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


def _record_campaign_id(record: dict) -> str | None:
    """Extract the campaign UUID from one analytics record.

    Instantly v2 has returned this under several keys across versions:
    `campaign_id`, `id`, sometimes `campaign`. Return the first non-empty
    value as a lowercase string so callers can compare without re-casing.
    """
    for key in ("campaign_id", "id", "campaign"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return None


class CampaignAnalyticsMismatch(RuntimeError):
    """Raised when the analytics endpoint returns no record matching the
    configured INSTANTLY_CAMPAIGN_ID. Carries the debug payload so the
    caller (script logger, Streamlit UI) can render it verbatim."""

    def __init__(self, message: str, debug: dict):
        super().__init__(message)
        self.debug = debug


@retry_api
async def _fetch_campaigns_analytics_raw(campaign_id: str) -> Any:
    """Hit GET /api/v2/campaigns/analytics with the campaign filter.

    Instantly's docs name this param `id` (UUID or comma-separated list).
    We send the same value under both `id` and `campaign_id` so a future
    rename of the param on Instantly's side doesn't silently fall back to
    "all campaigns" behavior — at least one of the two will be honored.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/campaigns/analytics",
            headers=_auth_headers(),
            params={"id": campaign_id, "campaign_id": campaign_id},
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_campaign_analytics(campaign_id: str) -> dict:
    """Fetch and STRICTLY filter analytics for one campaign id.

    Instantly's `/campaigns/analytics` endpoint silently ignores unknown
    query params and falls back to returning EVERY campaign in the
    workspace. If we picked `payload[0]` on that response we'd display
    account-wide numbers under the campaign's label — which is exactly
    the bug this rewrite fixes.

    Guarantees:
      - Returns the record whose campaign id matches `campaign_id`
        (case-insensitive) regardless of where Instantly puts the id key.
      - Raises `CampaignAnalyticsMismatch` if no record matches; the
        exception carries the list of returned campaign ids so the
        operator can see whether their env var is wrong or Instantly
        returned an unexpected shape.
      - Never aggregates, never sums, never returns multiple campaigns.
    """
    target = (campaign_id or "").strip().lower()
    if not target:
        raise CampaignAnalyticsMismatch(
            "INSTANTLY_CAMPAIGN_ID is empty — refusing to call /campaigns/analytics.",
            {"requested_campaign_id": campaign_id},
        )

    raw = await _fetch_campaigns_analytics_raw(campaign_id)

    if isinstance(raw, list):
        records = [r for r in raw if isinstance(r, dict)]
    elif isinstance(raw, dict):
        records = [raw]
    else:
        records = []

    returned_ids: list[str] = []
    matched: dict | None = None
    for rec in records:
        rid = _record_campaign_id(rec)
        if rid:
            returned_ids.append(rid)
        if rid == target:
            matched = rec
            break

    if matched is None:
        # The response either named no campaign id at all (a bare object
        # could be ambiguous) or named ones that don't match the env var.
        # If the API returned EXACTLY one record with no id key we accept
        # it — older Instantly responses for a campaign-scoped query do
        # exist in this shape. Anything else is a hard fail so account-
        # wide numbers never land in the snapshot.
        if len(records) == 1 and _record_campaign_id(records[0]) is None:
            return records[0]
        raise CampaignAnalyticsMismatch(
            f"Instantly /campaigns/analytics returned {len(records)} record(s); "
            f"none matched INSTANTLY_CAMPAIGN_ID={campaign_id}.",
            {
                "requested_campaign_id": campaign_id,
                "returned_campaign_ids": returned_ids,
                "record_count": len(records),
            },
        )
    return matched


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
    v2 also exposes positive_reply_count / opportunity_count / conversion_count
    when positive engagement has been recorded. Any missing key defaults to
    0 (or None for optional fields) — never raises.
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
        # Positive engagement — separate from generic reply_count.
        # Instantly may call these "positive_reply_count", "opportunity_count",
        # "opportunities", or "interested_count" depending on API version.
        # _coerce_optional_int returns None when the key is absent, which
        # lets the DB column stay NULL (informative: "not reported") vs 0
        # ("reported and zero").
        "positive_reply_count": _coerce_optional_int(
            raw.get("positive_reply_count")
            or raw.get("positive_replies_count")
            or raw.get("interested_count")
        ),
        "opportunity_count": _coerce_optional_int(
            raw.get("opportunity_count")
            or raw.get("opportunities_count")
            or raw.get("opportunities")
        ),
        "conversion_count": _coerce_optional_int(
            raw.get("conversion_count")
            or raw.get("conversions_count")
            or raw.get("conversions")
        ),
    }


async def sync_campaign_analytics() -> dict:
    """Pull analytics for the configured campaign only and persist a snapshot.

    Returns a dict combining the parsed metrics, the raw matched record,
    and a small debug bundle the UI can render to prove the snapshot is
    NOT an account-wide aggregate:

      requested_campaign_id   — value of INSTANTLY_CAMPAIGN_ID
      returned_campaign_ids   — every id Instantly handed back this call
      selected_campaign_id    — the id we matched and persisted
      selected_campaign_name  — campaign_name from the matched record

    Raises CampaignAnalyticsMismatch (subclass of RuntimeError) if none
    of the returned records matches the env var; the caller (script /
    UI) should surface it as a hard failure so account-wide totals never
    silently land in the KPI cards.
    """
    campaign_id = settings.instantly_campaign_id
    if not settings.instantly_api_key or not campaign_id:
        raise RuntimeError(
            "INSTANTLY_API_KEY or INSTANTLY_CAMPAIGN_ID not set — "
            "cannot sync campaign analytics"
        )

    raw_full = await _fetch_campaigns_analytics_raw(campaign_id)
    if isinstance(raw_full, list):
        all_records = [r for r in raw_full if isinstance(r, dict)]
    elif isinstance(raw_full, dict):
        all_records = [raw_full]
    else:
        all_records = []
    returned_ids = [
        rid for rid in (_record_campaign_id(r) for r in all_records) if rid
    ]

    target = campaign_id.strip().lower()
    matched: dict | None = None
    for rec in all_records:
        if _record_campaign_id(rec) == target:
            matched = rec
            break
    if matched is None and len(all_records) == 1 and not returned_ids:
        # Single anonymous record (legacy Instantly response shape) — accept.
        matched = all_records[0]

    if matched is None:
        raise CampaignAnalyticsMismatch(
            f"Instantly returned {len(all_records)} campaign record(s); "
            f"none matched INSTANTLY_CAMPAIGN_ID={campaign_id}. Refusing to "
            "store account-wide analytics.",
            {
                "requested_campaign_id": campaign_id,
                "returned_campaign_ids": returned_ids,
                "record_count": len(all_records),
            },
        )

    parsed = _parse_analytics(matched)
    selected_id = _record_campaign_id(matched) or campaign_id
    selected_name = (
        matched.get("campaign_name")
        or matched.get("name")
        or None
    )

    snapshot_id: int
    synced_at = datetime.utcnow()
    with session_scope() as session:
        # Store the env-var campaign id (not the API-returned one) so the
        # snapshot is keyed by what the operator configured. The matched
        # record itself goes into `raw` for full audit.
        snapshot = InstantlyAnalyticsSnapshot(
            campaign_id=campaign_id,
            raw=matched or {},
            synced_at=synced_at,
            **parsed,
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id

    log.info(
        "instantly_analytics_synced",
        extra={
            "requested_campaign_id": campaign_id,
            "selected_campaign_id": selected_id,
            "returned_campaign_ids": returned_ids,
            "snapshot_id": snapshot_id,
            **parsed,
        },
    )
    return {
        "snapshot_id": snapshot_id,
        "campaign_id": campaign_id,
        "synced_at": synced_at,
        "raw": matched,
        "requested_campaign_id": campaign_id,
        "returned_campaign_ids": returned_ids,
        "selected_campaign_id": selected_id,
        "selected_campaign_name": selected_name,
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
