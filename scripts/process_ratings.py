"""Walk SDR ratings into winning_examples.json and negative_examples.json.

Idempotent — re-running on the same DB state is a no-op. Mirrors
`scripts/pull_engagement.py` for the rating-based promotion path.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.feedback.learning import process_ratings
from src.logging_setup import setup_logging


def main() -> int:
    setup_logging()
    summary = process_ratings()
    print(
        "Ratings processed — "
        f"processed={summary['processed']}, "
        f"new_winners={summary['new_winners']}, "
        f"new_negatives={summary['new_negatives']}, "
        f"skipped_no_feedback={summary['skipped_no_feedback']}, "
        f"winners_total={summary['winners_total']}, "
        f"negatives_total={summary['negatives_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
