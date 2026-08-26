import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from src.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    """Serverless-safe engine options.

    On Vercel every request can land on a fresh, short-lived instance, so a
    per-instance connection pool just leaks Postgres connections. Use NullPool
    (connect/close per checkout) plus pre-ping there. Long-lived processes
    (Streamlit, the CLI, the worker) keep SQLAlchemy's default pool.
    """
    if os.environ.get("VERCEL"):
        return {"poolclass": NullPool, "pool_pre_ping": True}
    return {}


engine = create_engine(settings.database_url, echo=False, future=True, **_engine_kwargs())
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
# Tables added after the initial production deployment. `_apply_runtime_migrations`
# creates these via `ensure_tables()` so they exist even when `init_db()` ran
# against an older schema (e.g. the process didn't restart after a deploy).
_RUNTIME_NEW_TABLES: tuple[str, ...] = (
    "reply_drafts",
    "reply_threads",
    "lead_source_imports",   # Phase 7: external lead source import log
    "lead_signals",          # Hiring signal C-tier rescue layer
    "api_runs",              # Internal API (SalesOS) run tracking
)


def ensure_tables(*table_names: str) -> bool:
    """Create the named tables if they are missing from the live DB.

    Unlike ``init_db()``, this is NOT gated by ``_db_initialized``.  It is safe
    to call at any point in the request lifecycle — including from feature pages
    that need to ensure their tables exist before querying them.

    Idempotent: uses ``checkfirst=True`` so concurrent calls on a cold deploy
    are safe (the second call finds the table already exists and skips it).

    Returns True on success, False if creation failed (warning is logged and
    the exception is swallowed so the caller can decide how to degrade).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        from src import models as _m  # noqa: F401  register all models on Base.metadata
        _inspector = inspect(engine)
        existing = set(_inspector.get_table_names())
        to_create = [
            Base.metadata.tables[name]
            for name in table_names
            if name in Base.metadata.tables and name not in existing
        ]
        if to_create:
            Base.metadata.create_all(engine, tables=to_create, checkfirst=True)
            _log.info(
                "ensure_tables_created",
                extra={"created": [t.name for t in to_create]},
            )
        return True
    except Exception as exc:
        _log.warning(
            "ensure_tables_failed",
            extra={"tables": list(table_names), "error": f"{type(exc).__name__}: {exc}"},
        )
        return False


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
    # Phase 6: content type on WinningExample for workspace-scoped winners.
    ("winning_examples", "content_type", "VARCHAR(32)"),
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
    # Phase 7: external lead source API integration.
    # lead_source_config stores the per-workspace settings JSON (base URL,
    # api_key, client_slug, fetch limit, default filters, last fetch metadata).
    ("workspaces", "lead_source_config", "JSON"),
    # Lead-level source provenance columns.
    ("leads", "external_contact_id", "VARCHAR(256)"),  # primary dedup key (API UUID)
    ("leads", "external_source", "VARCHAR(64)"),        # "osp_lead_engine"
    ("leads", "external_client_slug", "VARCHAR(128)"),  # client slug at import time
    ("leads", "phone", "VARCHAR(64)"),                  # mobile_phone from external API
    ("leads", "lead_source_raw", "JSON"),               # full ContactOut payload
    # Import log extended fields.
    ("lead_source_imports", "base_url", "VARCHAR(512)"),
    ("lead_source_imports", "icp_filter", "VARCHAR(128)"),
    ("lead_source_imports", "status_filter", "VARCHAR(64)"),
    ("lead_source_imports", "include_suppressed", "BOOLEAN DEFAULT FALSE"),
    # Phase 8: evergreen automation — processing counts on the import log.
    ("lead_source_imports", "auto_run", "BOOLEAN DEFAULT FALSE"),
    ("lead_source_imports", "processed_count", "INTEGER DEFAULT 0"),
    ("lead_source_imports", "scored_count", "INTEGER DEFAULT 0"),
    ("lead_source_imports", "content_generated_count", "INTEGER DEFAULT 0"),
    ("lead_source_imports", "enrichment_skipped_count", "INTEGER DEFAULT 0"),
    # Hiring-signal run bookkeeping (added in the visibility hotfix). Older
    # lead_signals rows (created before these columns) read back as
    # status="not_started" via the model default on the next write.
    ("lead_signals", "status", "VARCHAR(16) DEFAULT 'not_started'"),
    ("lead_signals", "last_run_at", "TIMESTAMP"),
    ("lead_signals", "error", "TEXT"),
    # Source-signal layer: colleague's tier / tier_score from the lead engine
    # payload, stored separately from our local Score.tier.
    ("leads", "source_tier", "VARCHAR(16)"),
    ("leads", "source_tier_score", "FLOAT"),
    # Signal-first run trigger/poll flow — import-log bookkeeping.
    ("lead_source_imports", "triggered_run_id", "VARCHAR(128)"),
    ("lead_source_imports", "triggered_run_status", "VARCHAR(32)"),
    ("lead_source_imports", "source_signal_count", "INTEGER DEFAULT 0"),
]

# Postgres-only column widenings. SQLite ignores VARCHAR length caps so
# there's nothing to do for it — the same insert that succeeds on SQLite
# 1-line-tests is the one that errors on Streamlit Cloud's Postgres.
# (table, column, new type)
_RUNTIME_COLUMN_WIDENS_PG: list[tuple[str, str, str]] = [
    # "ready_for_approval" = 19 chars; original column was VARCHAR(16).
    ("prompt_recommendations", "status", "VARCHAR(32)"),
]


def _migrate_leads_email_composite_unique() -> None:
    """Phase 6: change leads unique index from (email) to (email, workspace_id).

    Allows the same email address to exist in different workspaces while
    still preventing duplicates within a workspace. Idempotent — only runs
    on PostgreSQL; SQLite tests use fresh schema each run.
    """
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.connect() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM pg_indexes "
                "WHERE tablename = 'leads' "
                "AND indexname = 'uq_leads_email_workspace'"
            )).scalar()
        if exists:
            return
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_email_key"
            ))
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_leads_email"))
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_email_workspace "
                "ON leads (email, workspace_id)"
            ))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "leads_email_composite_unique_migration_failed",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )


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

    Pass 0 — create new tables added after the initial production deployment.
             Uses ensure_tables() so this is safe even when called multiple
             times or when the process did not restart after a deploy.
    Pass 1 — ADD COLUMN for any model column missing from the live table.
             Cross-dialect: both SQLite and Postgres accept the plain
             `ALTER TABLE ... ADD COLUMN` form.
    Pass 2 — ALTER COLUMN TYPE for Postgres-only widening of VARCHARs that
             outgrew their original cap. Skipped on SQLite because SQLite's
             VARCHAR(N) doesn't enforce N anyway.
    """
    # Pass 0 — create tables that weren't present in the original schema.
    ensure_tables(*_RUNTIME_NEW_TABLES)

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
    # Pass 1c — Phase 6: change leads unique index from (email) to (email, workspace_id).
    _migrate_leads_email_composite_unique()

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
            migrate_json_winners_to_osp_db,
            seed_default_workspace,
        )
        seed_default_workspace()
        backfill_default_workspace_ids()
        backfill_osp_icp_config()
        migrate_json_winners_to_osp_db()
        # SalesOS integration: create the shared-Supabase contract tables only
        # when integration mode is on, so a standalone deployment never grows
        # unused salesos_* tables. The workers also ensure these lazily at boot.
        if settings.salesos_integration_mode:
            try:
                from src.integrations.salesos import ensure_salesos_tables
                ensure_salesos_tables()
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).warning("salesos_ensure_tables_failed")
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
