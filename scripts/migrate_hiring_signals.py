"""Hiring-signal migration — idempotent.

Creates the `lead_signals` table used by the C-tier hiring rescue layer.
Safe to run repeatedly: uses checkfirst so an existing table is left alone.

Run with:
    python scripts/migrate_hiring_signals.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from src.db import Base, engine
from src.logging_setup import setup_logging
from src.models import LeadSignal  # noqa: F401  registers the table on Base.metadata

log = logging.getLogger(__name__)

# Columns added after the table's first version (visibility hotfix).
_NEW_COLUMNS: list[tuple[str, str]] = [
    ("status", "VARCHAR(16) DEFAULT 'not_started'"),
    ("last_run_at", "TIMESTAMP"),
    ("error", "TEXT"),
]


def migrate(target_engine=None) -> dict:
    target_engine = target_engine or engine
    inspector = inspect(target_engine)
    existing = set(inspector.get_table_names())

    if "lead_signals" not in existing:
        Base.metadata.create_all(
            target_engine,
            tables=[Base.metadata.tables["lead_signals"]],
            checkfirst=True,
        )
        return {"table_created": True, "columns_added": []}

    # Table exists — add any missing columns idempotently.
    cols = {c["name"] for c in inspector.get_columns("lead_signals")}
    added: list[str] = []
    for name, ddl in _NEW_COLUMNS:
        if name in cols:
            continue
        with target_engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE lead_signals ADD COLUMN {name} {ddl}"))
        added.append(name)
    return {"table_created": False, "columns_added": added}


def main() -> int:
    setup_logging()
    summary = migrate()
    if summary["table_created"]:
        print("Hiring-signal migration applied. Table created: lead_signals")
    elif summary["columns_added"]:
        print("Hiring-signal migration applied. Columns added: "
              + ", ".join(summary["columns_added"]))
    else:
        print("Hiring-signal migration already applied — nothing to do.")
    log.info("hiring_signals_migration", extra=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
