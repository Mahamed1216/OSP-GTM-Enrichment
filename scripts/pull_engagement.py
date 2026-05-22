"""Sync Instantly engagement + roll up ratings + promote winners.

Designed to run from a scheduler (GitHub Actions cron, Task Scheduler, etc).
Has no Streamlit dependency — all imports come from ``src/``.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.feedback.engagement import sync_engagement
from src.feedback.learning import process_ratings, promote_winners
from src.logging_setup import setup_logging


async def main() -> None:
    setup_logging()
    init_db()

    sync = await sync_engagement()
    print(f"Engagement sync: synced={sync['synced']} failed={sync['failed']} total={sync['total']}")

    ratings = process_ratings()
    print(
        f"Rating rollup: processed={ratings['processed']} "
        f"new_winners={ratings['new_winners']} "
        f"new_negatives={ratings['new_negatives']} "
        f"skipped_no_feedback={ratings['skipped_no_feedback']}"
    )

    promo = promote_winners()
    print(f"Winner promotion: promoted={promo['promoted']} library_size={promo['library_size']}")

    ts_path = Path("data/last_engagement_sync.txt")
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ts_path.write_text(now.strftime("%Y-%m-%d %H:%M UTC"))
    print(f"[sync] Complete at {now.isoformat()}")


if __name__ == "__main__":
    asyncio.run(main())
