"""Run the full pipeline on all leads in the DB.

Default mode is DRY-RUN (no actual Instantly POST). Pass --live to deliver.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.logging_setup import setup_logging
from src.pipeline import run_pipeline_for_all


async def amain(live: bool) -> int:
    setup_logging()
    init_db()
    results = await run_pipeline_for_all(dry_run=not live)

    ok = sum(1 for r in results if r.error is None)
    delivered = sum(1 for r in results if r.delivery and r.delivery.delivered)
    skipped_tier = sum(1 for r in results if r.content_skipped)

    print(f"\nPipeline run: {ok}/{len(results)} succeeded, {delivered} delivered, {skipped_tier} skipped (low tier)")
    if not live:
        print("(Dry-run mode — pass --live to actually send.)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SDR pipeline on all leads.")
    parser.add_argument("--live", action="store_true",
                        help="Actually deliver to Instantly (default is dry-run)")
    args = parser.parse_args()
    return asyncio.run(amain(live=args.live))


if __name__ == "__main__":
    raise SystemExit(main())
