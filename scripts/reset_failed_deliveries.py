"""Phase 9b — one-shot cleaner for stale ERR delivery state.

Targets the "errored before ever touching Instantly" class: rows where
delivery_status='error' but no delivery_id ever landed. Anything with a
delivery_id is preserved (it actually reached Instantly and the error
state may still be diagnostic).

Run:  python scripts/reset_failed_deliveries.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from src.db import engine
from src.logging_setup import setup_logging
from src.models import GeneratedContent  # noqa: F401  registers table

log = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    sql = text(
        "UPDATE generated_contents "
        "SET delivery_status = NULL, error_message = NULL "
        "WHERE delivery_status = 'error' "
        "AND (delivery_id IS NULL OR delivery_id = '')"
    )
    with engine.begin() as conn:
        result = conn.execute(sql)
        rows = result.rowcount or 0

    log.info("reset_failed_deliveries", extra={"rows_affected": rows})
    print(f"Reset {rows} row(s) with stale ERR state (no delivery_id).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
