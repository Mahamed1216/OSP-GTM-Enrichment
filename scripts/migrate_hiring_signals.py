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

from sqlalchemy import inspect

from src.db import Base, engine
from src.logging_setup import setup_logging
from src.models import LeadSignal  # noqa: F401  registers the table on Base.metadata

log = logging.getLogger(__name__)


def migrate(target_engine=None) -> dict:
    target_engine = target_engine or engine
    existing = set(inspect(target_engine).get_table_names())
    if "lead_signals" in existing:
        return {"table_created": False}
    Base.metadata.create_all(
        target_engine,
        tables=[Base.metadata.tables["lead_signals"]],
        checkfirst=True,
    )
    return {"table_created": True}


def main() -> int:
    setup_logging()
    summary = migrate()
    if summary["table_created"]:
        print("Hiring-signal migration applied. Table created: lead_signals")
    else:
        print("Hiring-signal migration already applied — nothing to do.")
    log.info("hiring_signals_migration", extra=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
