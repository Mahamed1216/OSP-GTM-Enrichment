"""Phase 9 migration — idempotent.

Adds two columns to `generated_contents`:
  - delivery_status (String) — "sent" | "error" | "in_progress" | NULL
  - error_message (Text)     — failure body or repr(exc) on error

Run with:
    python scripts/migrate_phase9.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from src.db import engine
from src.logging_setup import setup_logging
from src.models import GeneratedContent  # noqa: F401  registers table

log = logging.getLogger(__name__)

_NEW_COLUMNS: list[tuple[str, str]] = [
    ("delivery_status", "VARCHAR(32)"),
    ("error_message", "TEXT"),
]


def migrate(target_engine=None) -> dict:
    target_engine = target_engine or engine
    inspector = inspect(target_engine)
    existing = {c["name"] for c in inspector.get_columns("generated_contents")}

    added: list[str] = []
    for name, sql_type in _NEW_COLUMNS:
        if name in existing:
            continue
        with target_engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE generated_contents ADD COLUMN {name} {sql_type}")
            )
        added.append(name)

    return {"added": added, "already_applied": not added}


def main() -> int:
    setup_logging()
    summary = migrate()
    if summary["already_applied"]:
        print("Phase 9 migration already applied — nothing to do.")
    else:
        print("Phase 9 migration applied. Columns added:")
        for col in summary["added"]:
            print(f"  + {col}")
    log.info("phase9_migration", extra=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
