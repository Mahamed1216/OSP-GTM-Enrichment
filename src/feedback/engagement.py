"""Pull engagement data from Instantly and persist to Engagement table."""
import logging
from datetime import datetime

import httpx
from sqlalchemy import select

from src.config import settings
from src.db import session_scope
from src.models import Engagement, GeneratedContent
from src.retry import retry_api

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@retry_api
async def _fetch_lead_metrics(delivery_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"https://api.instantly.ai/api/v2/leads/{delivery_id}",
            headers={"Authorization": f"Bearer {settings.instantly_api_key}"},
        )
        resp.raise_for_status()
        return resp.json()


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

    Instantly's exact field names vary by API version — this maps the common ones
    and treats any nonzero/truthy as "happened". `raw` is preserved verbatim.
    """
    return {
        "sent": _truthy(raw.get("emails_sent_count") or raw.get("sent")),
        "delivered": _truthy(raw.get("delivered") or raw.get("emails_sent_count")),
        "opened": _truthy(raw.get("email_open_count") or raw.get("opened")),
        "clicked": _truthy(raw.get("email_click_count") or raw.get("clicked")),
        "replied": _truthy(raw.get("email_reply_count") or raw.get("replied")),
        "bounced": _truthy(raw.get("bounced") or raw.get("email_bounce_count")),
        "raw": raw,
    }


async def sync_engagement() -> dict:
    """Iterate delivered emails and refresh their Engagement rows from Instantly."""
    with session_scope() as session:
        delivered = session.execute(
            select(GeneratedContent).where(
                GeneratedContent.delivered_at.is_not(None),
                GeneratedContent.delivery_id.is_not(None),
                GeneratedContent.kind == "email",
            )
        ).scalars().all()
        targets = [(c.id, c.delivery_id) for c in delivered]

    synced = failed = 0
    for content_id, delivery_id in targets:
        try:
            raw = await _fetch_lead_metrics(delivery_id)
        except Exception as exc:
            log.warning("engagement_fetch_failed", extra={
                "content_id": content_id,
                "delivery_id": delivery_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            failed += 1
            continue
        metrics = _parse_metrics(raw)
        with session_scope() as session:
            existing = session.execute(
                select(Engagement).where(Engagement.content_id == content_id)
            ).scalar_one_or_none()
            if existing:
                for k, v in metrics.items():
                    setattr(existing, k, v)
                existing.synced_at = datetime.utcnow()
            else:
                session.add(Engagement(content_id=content_id, **metrics))
        synced += 1

    log.info("engagement_sync_complete", extra={
        "synced": synced, "failed": failed, "total": len(targets),
    })
    return {"synced": synced, "failed": failed, "total": len(targets)}
