"""One-time migration from local SQLite to Supabase Postgres.

Usage:
    # default source = sqlite:///sdr.db, destination = $DATABASE_URL
    python scripts/migrate_to_production.py

    # override either side
    SQLITE_SOURCE_URL=sqlite:///./sdr.db \\
    DATABASE_URL=postgresql://user:pass@host:5432/postgres \\
    python scripts/migrate_to_production.py

Safe to run multiple times — every INSERT is wrapped with
``ON CONFLICT (id) DO NOTHING``. After the bulk copy, each table's primary-key
sequence is fast-forwarded past the max copied ID so future inserts from the
Streamlit app don't collide.

The destination URL is read from ``DATABASE_URL`` (the same env var the live
app uses); this script refuses to run if that points back at SQLite, since
that would defeat the point of migrating.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

load_dotenv()

from src.db import Base  # noqa: E402  registers metadata via models import
from src.logging_setup import setup_logging  # noqa: E402
from src.models import (  # noqa: E402
    ContentRating,
    Engagement,
    Enrichment,
    GeneratedContent,
    Lead,
    Score,
    WinningExample,
)

log = logging.getLogger(__name__)

# Order matters for FK satisfaction even with ON CONFLICT DO NOTHING — children
# need their parent IDs visible when we insert them.
ORDERED_MODELS = [
    Lead,
    Enrichment,
    Score,
    GeneratedContent,
    Engagement,
    ContentRating,
    WinningExample,
]


def _get_source_url() -> str:
    return os.environ.get("SQLITE_SOURCE_URL", "sqlite:///sdr.db")


def _get_destination_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit(
            "DATABASE_URL not set. Point it at your Supabase Postgres "
            "connection string and re-run."
        )
    if url.startswith("sqlite"):
        raise SystemExit(
            f"DATABASE_URL is still SQLite ({url!r}). Set it to your "
            "Supabase postgresql:// URL before running this migration."
        )
    return url


def _ensure_destination_schema(dest_engine) -> None:
    """Create every table from src.models on the destination if absent."""
    Base.metadata.create_all(dest_engine)


def _reset_sequence(session: Session, table_name: str) -> None:
    """Fast-forward the PK sequence past the max copied id.

    Postgres autoincrement is driven by a sequence (``<table>_id_seq``); since
    we insert with explicit IDs from SQLite, the sequence stays at its initial
    value 1, and the next implicit insert from the app would collide. This
    setval makes the sequence's nextval = max(id) + 1.
    """
    sql = text(
        f"""
        SELECT setval(
            pg_get_serial_sequence('{table_name}', 'id'),
            COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1,
            false
        )
        """
    )
    session.execute(sql)


def _copy_table(
    model,
    src_session: Session,
    dest_session: Session,
) -> tuple[int, int]:
    """Copy every row of one model. Returns (read, inserted_or_skipped)."""
    rows = src_session.execute(select(model)).scalars().all()
    if not rows:
        return 0, 0
    table = model.__table__
    payloads = []
    for row in rows:
        payloads.append({c.name: getattr(row, c.name) for c in table.columns})
    stmt = pg_insert(table).values(payloads).on_conflict_do_nothing(
        index_elements=["id"]
    )
    result = dest_session.execute(stmt)
    inserted = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else len(payloads)
    return len(rows), inserted


def main() -> int:
    setup_logging()
    src_url = _get_source_url()
    dest_url = _get_destination_url()

    print(f"Source:      {src_url}")
    print(f"Destination: {dest_url.split('@')[-1] if '@' in dest_url else dest_url}")
    print()

    src_engine = create_engine(src_url, future=True)
    dest_engine = create_engine(dest_url, future=True)

    print("Creating destination schema (idempotent) …")
    _ensure_destination_schema(dest_engine)

    totals: list[tuple[str, int, int]] = []
    with Session(src_engine) as src_session, Session(dest_engine) as dest_session:
        for model in ORDERED_MODELS:
            tname = model.__tablename__
            try:
                read, inserted = _copy_table(model, src_session, dest_session)
            except Exception as exc:
                dest_session.rollback()
                print(f"  ✗ {tname}: failed — {exc}")
                log.error("migrate_table_failed", extra={"table": tname, "error": str(exc)})
                raise
            dest_session.commit()
            if read:
                _reset_sequence(dest_session, tname)
                dest_session.commit()
            totals.append((tname, read, inserted))
            print(f"  ✓ {tname}: read={read}, inserted_new={inserted}")

    print()
    print("Migration complete.")
    print("Summary:")
    for tname, read, inserted in totals:
        print(f"  {tname:>22}  read={read:>5}  inserted_new={inserted:>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
