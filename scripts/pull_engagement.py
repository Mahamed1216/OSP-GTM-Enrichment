"""Sync Instantly engagement + promote winners. Run on a schedule (cron / Task Scheduler)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.feedback.engagement import sync_engagement
from src.feedback.learning import promote_winners
from src.logging_setup import setup_logging


async def main() -> None:
    setup_logging()
    init_db()
    sync = await sync_engagement()
    print(f"Engagement sync: synced={sync['synced']} failed={sync['failed']} total={sync['total']}")
    promo = promote_winners()
    print(f"Winner promotion: promoted={promo['promoted']} library_size={promo['library_size']}")


if __name__ == "__main__":
    asyncio.run(main())
