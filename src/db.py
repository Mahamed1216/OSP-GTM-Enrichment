from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Process-level flag — True once init_db() has completed for this Python process.
# Unlike st.session_state (which is per browser session and can be rehydrated by
# Streamlit Cloud across server restarts), this flag resets to False whenever the
# OS process is restarted, so init_db() always runs on a fresh deploy.
_db_initialized: bool = False


def is_db_initialized() -> bool:
    """Return True if init_db() has completed successfully for this process."""
    return _db_initialized


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
    # Which raw JSON key / fallback path produced the positive counts.
    ("instantly_analytics_snapshots", "raw_positive_reply_source", "VARCHAR(128)"),
    ("instantly_analytics_snapshots", "raw_opportunity_source", "VARCHAR(128)"),
    # FK to the snapshot a PromptRecommendation was based on, for staleness
    # detection when a newer sync arrives before the operator acts.
    ("prompt_recommendations", "analytics_snapshot_id", "INTEGER"),
    # Phase 4 hotfix: workspace-scoped ICP/company settings on the workspace row.
    ("workspaces", "icp_config", "JSON"),
    # Phase 2: workspace_id added to every workspace-scoped table.
    # Nullable so existing rows survive migration; backfilled to the default
    # OSP workspace by backfill_default_workspace_ids() called from init_db().
    # No FK constraint here — the runtime migration style does not enforce FKs
    # in ALTER TABLE statements; ORM models carry the semantic relationship.
    ("leads", "workspace_id", "INTEGER"),
    ("enrichments", "workspace_id", "INTEGER"),
    ("scores", "workspace_id", "INTEGER"),
    ("generated_contents", "workspace_id", "INTEGER"),
    ("engagements", "workspace_id", "INTEGER"),
    ("content_ratings", "workspace_id", "INTEGER"),
    ("winning_examples", "workspace_id", "INTEGER"),
    ("instantly_analytics_snapshots", "workspace_id", "INTEGER"),
    ("prompt_recommendations", "workspace_id", "INTEGER"),
    ("prompt_configs", "workspace_id", "INTEGER"),
]

# Postgres-only column widenings. SQLite ignores VARCHAR length caps so
# there's nothing to do for it — the same insert that succeeds on SQLite
# 1-line-tests is the one that errors on Streamlit Cloud's Postgres.
# (table, column, new type)
_RUNTIME_COLUMN_WIDENS_PG: list[tuple[str, str, str]] = [
    # "ready_for_approval" = 19 chars; original column was VARCHAR(16).
    ("prompt_recommendations", "status", "VARCHAR(32)"),
]


def _migrate_prompt_configs_composite_unique() -> None:
    """Phase 4: change prompt_configs unique index from (channel) to (channel, workspace_id).

    Idempotent — checks if composite index already exists before making changes.
    Only runs on PostgreSQL; SQLite tests use fresh schema each run.
    """
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.connect() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM pg_indexes "
                "WHERE tablename = 'prompt_configs' "
                "AND indexname = 'uq_prompt_configs_channel_workspace'"
            )).scalar()
        if exists:
            return
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE prompt_configs "
                "DROP CONSTRAINT IF EXISTS prompt_configs_channel_key"
            ))
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_prompt_configs_channel"))
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_configs_channel_workspace "
                "ON prompt_configs (channel, workspace_id)"
            ))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "prompt_configs_composite_unique_migration_failed",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )


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
    # Postgres supports ADD COLUMN IF NOT EXISTS, which makes concurrent
    # startup safe (two workers both see the column absent and both try to
    # add it — the second one is a harmless no-op instead of an error).
    # SQLite does not support IF NOT EXISTS in this form, so we keep the
    # Python-level pre-check for SQLite.
    use_if_not_exists = engine.dialect.name == "postgresql"
    for table_name, col_name, ddl_type in _RUNTIME_COLUMN_ADDS:
        if table_name not in existing_tables:
            continue  # create_all just made it with the new column already in place
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        if col_name in cols:
            continue
        if use_if_not_exists:
            ddl = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {ddl_type}"
        else:
            ddl = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {ddl_type}"
        with engine.begin() as conn:
            conn.execute(text(ddl))

    # Pass 1b — Phase 4: change prompt_configs unique index to composite.
    _migrate_prompt_configs_composite_unique()

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
    """Create all tables, run column migrations, seed the default workspace.

    Guarded by a process-level flag so repeated calls are instant no-ops.
    The flag is set at the *start* of the work (not the end) so that
    workspace helpers called from within this function (e.g. via
    backfill_default_workspace_ids → get_default_workspace_id) do not
    recursively trigger init_db() again.  The flag is cleared on failure
    so the next request can retry.
    """
    global _db_initialized
    if _db_initialized:
        return
    _db_initialized = True  # Prevent re-entry; cleared below if we fail.
    try:
        from src import models  # noqa: F401  registers tables on Base.metadata
        Base.metadata.create_all(engine)
        _apply_runtime_migrations()
        # Lazy imports avoid circular dependency: src.workspace imports src.db.
        from src.workspace import (
            backfill_default_workspace_ids,
            backfill_osp_icp_config,
            seed_default_workspace,
        )
        seed_default_workspace()
        backfill_default_workspace_ids()
        backfill_osp_icp_config()
    except Exception:
        _db_initialized = False  # Allow the next caller to retry.
        raise


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
