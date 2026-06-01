from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# (table_name, column_name, DDL fragment). Kept ordered so a fresh DB and an
# existing DB both end up identical. Each entry must be idempotent:
# `_apply_runtime_migrations` skips when the column already exists.
_RUNTIME_COLUMN_ADDS: list[tuple[str, str, str]] = [
    # Added so bulk-regen resume can fingerprint the prompt that produced
    # a row, rather than relying on the unchanging prompt_version constant.
    ("generated_contents", "prompt_fingerprint", "VARCHAR(64)"),
    # Self-improvement-loop audit columns. Added in the schema-mismatch
    # fix after a DataError on Streamlit Cloud Postgres — the loop's
    # status vocabulary outgrew the original VARCHAR(16) `status` column
    # and the audit fields below were never persisted at all.
    ("prompt_recommendations", "loop_status", "VARCHAR(32)"),
    ("prompt_recommendations", "confidence", "VARCHAR(16)"),
    ("prompt_recommendations", "proposed_addendum", "TEXT"),
    # JSON in raw DDL: Postgres treats it natively, SQLite stores it
    # under its dynamic-typing rules and accepts the keyword without
    # error since SQLite 3.7. SQLAlchemy's `JSON` type handles the
    # serialisation on read/write so both backends round-trip cleanly.
    ("prompt_recommendations", "metric_snapshot", "JSON"),
    ("prompt_recommendations", "rejected_at", "TIMESTAMP"),
    ("prompt_recommendations", "drafted_at", "TIMESTAMP"),
    # PromptConfig audit columns added in the persistence/cleanup fix.
    ("prompt_configs", "prompt_version", "VARCHAR(32)"),
    ("prompt_configs", "prompt_fingerprint", "VARCHAR(64)"),
    ("prompt_configs", "updated_by", "VARCHAR(64)"),
    # is_active defaults to TRUE so existing rows behave the same as new ones.
    ("prompt_configs", "is_active", "BOOLEAN DEFAULT TRUE"),
    # Buyer-account discovery. JSON column holds the BuyerAccountResult
    # dump; left null for pre-migration enrichment rows (the email
    # generator falls back to the buyer-segment branch when this is
    # null, so old rows don't break).
    ("enrichments", "buyer_accounts", "JSON"),
    # Instantly positive-reply / opportunity / conversion counts — tracked
    # separately from total reply_count. NULL for rows created before this
    # column was added; the raw JSON field still holds the original values.
    ("instantly_analytics_snapshots", "positive_reply_count", "INTEGER"),
    ("instantly_analytics_snapshots", "opportunity_count", "INTEGER"),
    ("instantly_analytics_snapshots", "conversion_count", "INTEGER"),
    # FK to the snapshot a PromptRecommendation was based on, for staleness
    # detection when a newer sync arrives before the operator acts.
    ("prompt_recommendations", "analytics_snapshot_id", "INTEGER"),
]

# Postgres-only column widenings. SQLite ignores VARCHAR length caps so
# there's nothing to do for it — the same insert that succeeds on SQLite
# 1-line-tests is the one that errors on Streamlit Cloud's Postgres.
# (table, column, new type)
_RUNTIME_COLUMN_WIDENS_PG: list[tuple[str, str, str]] = [
    # "ready_for_approval" = 19 chars; original column was VARCHAR(16).
    ("prompt_recommendations", "status", "VARCHAR(32)"),
]


def _apply_runtime_migrations() -> None:
    """Idempotently bring existing tables in line with the current model.

    Two passes:
      1. ADD COLUMN for any model column missing from the live table.
         Cross-dialect: both SQLite and Postgres accept the plain
         `ALTER TABLE ... ADD COLUMN` form.
      2. ALTER COLUMN TYPE for Postgres-only widening of VARCHARs that
         outgrew their original cap. Skipped on SQLite because SQLite's
         VARCHAR(N) doesn't enforce N anyway.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # Pass 1 — add missing columns.
    for table_name, col_name, ddl_type in _RUNTIME_COLUMN_ADDS:
        if table_name not in existing_tables:
            continue  # create_all just made it with the new column already in place
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        if col_name in cols:
            continue
        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {ddl_type}")
            )

    # Pass 2 — widen string columns on Postgres.
    if engine.dialect.name == "postgresql":
        # Re-inspect after pass 1 in case a column we want to widen was
        # just added (no-op widen, but harmless).
        inspector = inspect(engine)
        for table_name, col_name, new_type in _RUNTIME_COLUMN_WIDENS_PG:
            if table_name not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table_name)}
            if col_name not in cols:
                continue
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ALTER COLUMN {col_name} TYPE {new_type}"
                    )
                )


def init_db() -> None:
    from src import models  # noqa: F401  registers tables on Base.metadata
    Base.metadata.create_all(engine)
    _apply_runtime_migrations()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
