"""Phase 5 migration — idempotent.

1. Creates the new `content_ratings` table via Base.metadata.create_all().
2. Adds `superseded_by_id` column to `generated_contents` if it does not yet
   exist (raw ALTER TABLE — SQLAlchemy does not auto-add columns to existing
   tables).

Safe to run twice. Run with:
    python scripts/migrate_phase5.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from src.db import Base, engine
from src.logging_setup import setup_logging
from src.models import ContentRating, GeneratedContent  # noqa: F401  registers tables

log = logging.getLogger(__name__)


def migrate(target_engine=None) -> dict:
    """Apply Phase 5 schema changes. Returns a summary dict."""
    target_engine = target_engine or engine
    summary = {
        "content_ratings_created": False,
        "superseded_by_id_added": False,
        "already_applied": False,
    }

    inspector = inspect(target_engine)
    existing_tables = set(inspector.get_table_names())
    needed_create = "content_ratings" not in existing_tables

    Base.metadata.create_all(target_engine)
    summary["content_ratings_created"] = needed_create

    inspector = inspect(target_engine)
    columns = {c["name"] for c in inspector.get_columns("generated_contents")}
    if "superseded_by_id" not in columns:
        with target_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE generated_contents "
                    "ADD COLUMN superseded_by_id INTEGER "
                    "REFERENCES generated_contents(id)"
                )
            )
        summary["superseded_by_id_added"] = True

    summary["already_applied"] = (
        not summary["content_ratings_created"]
        and not summary["superseded_by_id_added"]
    )
    return summary


def main() -> int:
    setup_logging()
    summary = migrate()
    if summary["already_applied"]:
        print("Phase 5 migration already applied — nothing to do.")
    else:
        print("Phase 5 migration applied:")
        print(f"  content_ratings table created: {summary['content_ratings_created']}")
        print(f"  superseded_by_id column added: {summary['superseded_by_id_added']}")
    log.info("phase5_migration", extra=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
