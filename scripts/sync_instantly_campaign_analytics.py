"""Scheduled campaign-analytics sync — entrypoint for GitHub Actions.

This is the SAME code path the Engagement page's "Sync engagement from
Instantly" button uses: `sync_campaign_analytics()` for the top-of-page
KPIs (Instantly is source of truth), then `sync_engagement()` for the
per-lead rows the recent-engagement list and prompt-experiment tracker
read from.

Print format is deliberately stable so the GitHub Actions log can be
grepped for `campaign_id=...`, `instantly_opens=...`, etc. The Engagement
page's "Source: Instantly campaign analytics" KPI row reads the snapshot
this script writes, so a successful run here is what makes the dashboard
numbers match Instantly.

Env vars required (set as repo secrets in GitHub):
  - DATABASE_URL
  - INSTANTLY_API_KEY
  - INSTANTLY_CAMPAIGN_ID

The lead-level sync is included AFTER the analytics snapshot so a
failure in the slower per-lead pass cannot rob the top-line KPI of its
fresh snapshot.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.feedback.engagement import sync_campaign_analytics, sync_engagement
from src.feedback.learning import process_ratings, promote_winners
from src.logging_setup import setup_logging


def _safe_rate(num: int, denom: int) -> str:
    if denom <= 0:
        return "n/a"
    return f"{(num / denom) * 100:.2f}%"


async def main() -> int:
    setup_logging()
    init_db()

    started = datetime.now(timezone.utc)
    print(f"[campaign-analytics-sync] start={started.isoformat()}")

    # 1) Campaign analytics — Instantly is source of truth for the
    # top-of-page KPI cards. This MUST run before the per-lead sync so a
    # later failure doesn't suppress the snapshot.
    try:
        analytics = await sync_campaign_analytics()
    except Exception as exc:
        print(
            f"[campaign-analytics-sync] FAILED analytics: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    campaign_id = analytics.get("campaign_id")
    contacted = int(analytics.get("contacted_count") or 0)
    sent = int(analytics.get("emails_sent_count") or 0)
    opens = int(analytics.get("open_count") or 0)
    replies = int(analytics.get("reply_count") or 0)
    bounces = int(analytics.get("bounced_count") or 0)
    synced_at = analytics.get("synced_at")
    synced_at_str = (
        synced_at.isoformat() if isinstance(synced_at, datetime) else str(synced_at)
    )

    # Stable key=value log lines — exactly the fields the task requested,
    # so an Actions log grep can confirm each metric without parsing prose.
    print(f"[campaign-analytics-sync] campaign_id={campaign_id}")
    print(f"[campaign-analytics-sync] instantly_sent_or_sequence_started={contacted}")
    print(f"[campaign-analytics-sync] instantly_emails_sent={sent}")
    print(f"[campaign-analytics-sync] instantly_opens={opens}")
    print(f"[campaign-analytics-sync] instantly_replies={replies}")
    print(f"[campaign-analytics-sync] instantly_bounces={bounces}")
    print(f"[campaign-analytics-sync] open_rate={_safe_rate(opens, sent)}")
    print(f"[campaign-analytics-sync] reply_rate={_safe_rate(replies, sent)}")
    print(f"[campaign-analytics-sync] bounce_rate={_safe_rate(bounces, sent)}")
    print(f"[campaign-analytics-sync] synced_at={synced_at_str}")
    print(f"[campaign-analytics-sync] snapshot_id={analytics.get('snapshot_id')}")

    # 2) Per-lead engagement sync — optional, runs AFTER analytics so a
    # failure here can't suppress the KPI snapshot. Same code path the
    # Engagement page's manual button calls second.
    try:
        per_lead = await sync_engagement()
        print(
            "[campaign-analytics-sync] per_lead_sync "
            f"synced={per_lead['synced']} failed={per_lead['failed']} "
            f"unknown={per_lead.get('unknown', 0)} total={per_lead['total']}"
        )
    except Exception as exc:
        print(
            f"[campaign-analytics-sync] per-lead sync failed (non-fatal): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    # 3) Ratings + winners rollup — best effort, also non-fatal.
    try:
        ratings = process_ratings()
        print(
            "[campaign-analytics-sync] ratings "
            f"processed={ratings['processed']} "
            f"new_winners={ratings['new_winners']} "
            f"new_negatives={ratings['new_negatives']}"
        )
        promo = promote_winners()
        print(
            f"[campaign-analytics-sync] winners "
            f"promoted={promo['promoted']} library_size={promo['library_size']}"
        )
    except Exception as exc:
        print(
            f"[campaign-analytics-sync] ratings/winners rollup failed (non-fatal): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    # Timestamp file — kept for backward-compat with older UI captions
    # that read data/last_engagement_sync.txt directly.
    ts_path = Path("data/last_engagement_sync.txt")
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ts_path.write_text(now.strftime("%Y-%m-%d %H:%M UTC"))

    duration = (now - started).total_seconds()
    print(f"[campaign-analytics-sync] done duration_sec={duration:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
