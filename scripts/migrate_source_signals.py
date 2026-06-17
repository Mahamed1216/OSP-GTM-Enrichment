"""Source-signal migration — idempotent.

Adds the source-tier columns to `leads` used by the imported-enrichment layer:
  - source_tier        (VARCHAR) — colleague's tier from the lead engine payload
  - source_tier_score  (FLOAT)   — colleague's tier_score

The imported signals themselves are stored in the existing `lead_source_raw`
JSON column (full payload) plus a normalized `lead_signals` row
(signal_type="source_import"), so no new table is required.

Run with:
    python scripts/migrate_source_signals.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from src.db import engine
from src.logging_setup import setup_logging
from src.models import Lead  # noqa: F401  registers the table

log = logging.getLogger(__name__)

_NEW_COLUMNS: list[tuple[str, str]] = [
    ("source_tier", "VARCHAR(16)"),
    ("source_tier_score", "FLOAT"),
]


def migrate(target_engine=None) -> dict:
    target_engine = target_engine or engine
    inspector = inspect(target_engine)
    cols = {c["name"] for c in inspector.get_columns("leads")}
    added: list[str] = []
    for name, ddl in _NEW_COLUMNS:
        if name in cols:
            continue
        with target_engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE leads ADD COLUMN {name} {ddl}"))
        added.append(name)
    return {"columns_added": added}


def main() -> int:
    setup_logging()
    summary = migrate()
    if summary["columns_added"]:
        print("Source-signal migration applied. Columns added: "
              + ", ".join(summary["columns_added"]))
    else:
        print("Source-signal migration already applied — nothing to do.")
    log.info("source_signals_migration", extra=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
