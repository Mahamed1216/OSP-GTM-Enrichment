"""Reliable engagement sync entrypoint for external schedulers.

Runs three things, in order, with clear logs at each step:

  1. Campaign analytics snapshot from Instantly (`/campaigns/analytics`).
     This is the source of truth for the Engagement page KPIs.
  2. Per-lead engagement sync (paginated `/leads/list`). Still useful
     for the lead-level "did this person open?" data the lead detail
     page renders.
  3. Rating rollup + winner promotion.

Designed for the GitHub Actions cron in `.github/workflows/engagement_sync.yml`.
No Streamlit dependency — only `src/`. Exits non-zero on failure so
the Actions run is flagged red.

Env vars required:
  - DATABASE_URL
  - INSTANTLY_API_KEY
  - INSTANTLY_CAMPAIGN_ID
  - ANTHROPIC_API_KEY (for reply-sentiment processing inside rollup)
  - SYNC_SECRET (optional — only enforced when this script is later
    fronted by an HTTP endpoint; currently informational so the
    workflow can pass it without breaking).
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.feedback.engagement import sync_campaign_analytics, sync_engagement
from src.feedback.learning import process_ratings, promote_winners
from src.logging_setup import setup_logging


def _check_secret() -> None:
    """If SYNC_SECRET is set in this process AND a value is provided via
    the SYNC_SECRET_PROVIDED env (set by the HTTP wrapper, when present),
    verify they match. Local CLI / GitHub Actions usage skips this check.
    """
    expected = os.environ.get("SYNC_SECRET")
    provided = os.environ.get("SYNC_SECRET_PROVIDED")
    if expected and provided and expected != provided:
        print("[sync] SYNC_SECRET mismatch — refusing to run.", file=sys.stderr)
        sys.exit(2)


async def main() -> None:
    _check_secret()
    setup_logging()
    init_db()

    started = datetime.now(timezone.utc)
    print(f"[sync] Start: {started.isoformat()}")

    # 1) Campaign analytics — Instantly is source of truth for KPIs.
    try:
        analytics = await sync_campaign_analytics()
        print(
            "[sync] Analytics: "
            f"contacted={analytics['contacted_count']} "
            f"sent={analytics['emails_sent_count']} "
            f"opens={analytics['open_count']} "
            f"replies={analytics['reply_count']} "
            f"bounces={analytics['bounced_count']}"
        )
    except Exception as exc:
        print(f"[sync] Analytics FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

    # 2) Per-lead engagement upsert.
    try:
        per_lead = await sync_engagement()
        print(
            "[sync] Per-lead: "
            f"synced={per_lead['synced']} failed={per_lead['failed']} "
            f"unknown={per_lead.get('unknown', 0)} total={per_lead['total']}"
        )
    except Exception as exc:
        print(f"[sync] Per-lead sync FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

    # 3) Ratings + winners promotion. Best-effort — a failure here doesn't
    #    invalidate the analytics we already persisted.
    try:
        ratings = process_ratings()
        print(
            f"[sync] Ratings: processed={ratings['processed']} "
            f"new_winners={ratings['new_winners']} "
            f"new_negatives={ratings['new_negatives']}"
        )
        promo = promote_winners()
        print(
            f"[sync] Winners: promoted={promo['promoted']} "
            f"library_size={promo['library_size']}"
        )
    except Exception as exc:
        print(
            f"[sync] Ratings/winners rollup failed (non-fatal): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    # Timestamp file (kept for backward-compat with older UI captions).
    ts_path = Path("data/last_engagement_sync.txt")
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ts_path.write_text(now.strftime("%Y-%m-%d %H:%M UTC"))
    print(f"[sync] Done at {now.isoformat()} (duration {(now - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    asyncio.run(main())
