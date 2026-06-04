"""Workspace helpers — Phase 1 + Phase 2.

Phase 1: workspace lookup, campaign-ID resolver, default OSP seeding.
Phase 2: workspace_id backfill, per-table coverage stats, insert-time
         helpers for safe workspace assignment on new rows.

Phase 2 design rules:
  - All workspace_id columns are nullable; no inserts are broken.
  - backfill_default_workspace_ids() is idempotent and called from init_db().
  - current_workspace_id_or_default() always returns the default workspace
    because workspace switching is not yet available (Phase 3).
  - App query filtering by workspace_id is NOT added here; that is Phase 3.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from src.config import settings
from src.db import session_scope
from src.models import Workspace

log = logging.getLogger(__name__)

_DEFAULT_WORKSPACE_NAME = "OSP"
_DEFAULT_WORKSPACE_SLUG = "osp"

# All tables that carry workspace_id in Phase 2. Used by backfill and stats.
# `workspaces` itself is excluded — it IS the workspace table.
# `winning_examples` is the legacy DB model (WinningExample); the current
#   JSON-based winners library (data/winning_examples.json) is not in the DB.
# sync_logs / delivery_logs / winners_library do not exist in this schema.
WORKSPACE_SCOPED_TABLES: tuple[str, ...] = (
    "leads",
    "enrichments",
    "scores",
    "generated_contents",
    "engagements",
    "content_ratings",
    "winning_examples",
    "instantly_analytics_snapshots",
    "prompt_recommendations",
    "prompt_configs",
)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def ensure_workspace_schema() -> bool:
    """Ensure the workspaces table and workspace_id columns exist (DDL only).

    Fast path  — if init_db() has already completed for this process, this
    returns True in a single bool check.

    Medium path — if init_db() hasn't run yet but the workspaces table is
    already present (e.g. the process restarted but the DB was not dropped),
    returns True without re-running any DDL.

    Slow path  — if the workspaces table is genuinely absent (first deploy
    against a fresh Postgres DB before init_db() has run), runs
    Base.metadata.create_all + _apply_runtime_migrations so workspace
    queries don't crash with "relation does not exist".

    Seeding the default workspace is intentionally excluded here — that is
    init_db()'s responsibility, called unconditionally from app/main.py
    before any page renders.  Excluding seeding keeps this function safe to
    call from library helpers: it will not create workspace rows in tests
    that deliberately start with an empty workspaces table.

    Returns True when the schema is ready, False if an unexpected error
    prevented table creation.
    """
    try:
        from src.db import is_db_initialized, engine as _engine
        if is_db_initialized():
            return True  # init_db() already ran — all tables and columns are ready.
        from sqlalchemy import inspect as _inspect
        if "workspaces" in set(_inspect(_engine).get_table_names()):
            return True  # table exists; may be unseeded — that's fine, return None below.
        # Table is genuinely missing: create it (and add workspace_id columns to
        # existing tables) without seeding workspace rows.
        from src import models as _models  # noqa: F401  register on Base.metadata
        from src.db import Base as _Base, _apply_runtime_migrations
        _Base.metadata.create_all(_engine)
        _apply_runtime_migrations()
        return True
    except Exception as exc:
        log.warning(
            "ensure_workspace_schema_failed",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        return False


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_default_workspace() -> None:
    """Create the default OSP workspace if no workspaces exist yet.

    Idempotent — safe to call on every app startup. Does nothing when at
    least one workspace row already exists, so re-running init_db() never
    creates duplicate workspaces.

    The instantly_campaign_id is pulled from the INSTANTLY_CAMPAIGN_ID env
    var at seed time. If the var is blank the column is left NULL and the
    campaign-ID resolver falls back to the env var at call time (so a late
    .env edit is still picked up without re-seeding).
    """
    with session_scope() as session:
        existing = session.execute(
            select(Workspace).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return  # already seeded — nothing to do

        campaign_id = (settings.instantly_campaign_id or "").strip() or None
        workspace = Workspace(
            name=_DEFAULT_WORKSPACE_NAME,
            slug=_DEFAULT_WORKSPACE_SLUG,
            is_default=True,
            is_active=True,
            instantly_campaign_id=campaign_id,
        )
        session.add(workspace)

    log.info(
        "workspace_seeded",
        extra={
            "name": _DEFAULT_WORKSPACE_NAME,
            "slug": _DEFAULT_WORKSPACE_SLUG,
            "campaign_id_set": campaign_id is not None,
        },
    )


# ---------------------------------------------------------------------------
# Lookups — all return plain dicts so callers never touch detached ORM objects.
# ---------------------------------------------------------------------------

def _row_to_dict(row: Workspace) -> dict[str, Any]:
    """Read all Workspace attributes inside a session and return a plain dict."""
    return {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "is_default": row.is_default,
        "is_active": row.is_active,
        "instantly_campaign_id": row.instantly_campaign_id,
        "instantly_api_key": row.instantly_api_key,
        "notes": row.notes,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def get_default_workspace() -> dict[str, Any] | None:
    """Return the workspace marked is_default=True as a plain dict, or None."""
    ensure_workspace_schema()
    with session_scope() as session:
        row = session.execute(
            select(Workspace)
            .where(Workspace.is_default.is_(True))
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return _row_to_dict(row)


def get_or_create_default_workspace() -> dict[str, Any]:
    """Return the default workspace, seeding it first if absent.

    Guaranteed to return a dict (never None) after this call. Safe to
    call multiple times — seeding is idempotent.
    """
    seed_default_workspace()
    result = get_default_workspace()
    if result is None:
        # Extremely unlikely — seed just ran; guard for DB edge cases.
        raise RuntimeError(
            "Default workspace not found even after seeding. "
            "Check the workspaces table for constraint violations."
        )
    return result


def get_workspace_by_id(workspace_id: int) -> dict[str, Any] | None:
    """Return a workspace dict by primary key, or None if not found."""
    ensure_workspace_schema()
    with session_scope() as session:
        row = session.get(Workspace, workspace_id)
        if row is None:
            return None
        return _row_to_dict(row)


def get_active_workspaces() -> list[dict[str, Any]]:
    """Return all active workspaces ordered by id ascending."""
    ensure_workspace_schema()
    with session_scope() as session:
        rows = session.execute(
            select(Workspace)
            .where(Workspace.is_active.is_(True))
            .order_by(Workspace.id.asc())
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Campaign-ID resolver — preserves Phase 1 env-based behavior.
# ---------------------------------------------------------------------------

def get_campaign_id_for_workspace(workspace_id: int | None = None) -> str | None:
    """Resolve the Instantly campaign ID for a workspace.

    Lookup priority:
      1. workspace.instantly_campaign_id (if non-empty for the given workspace)
      2. settings.instantly_campaign_id  (env-var fallback — unchanged from Phase 0)
      3. None — caller decides whether to raise or skip the Instantly action.

    When workspace_id is None, the default workspace is used. This means all
    existing code that reads settings.instantly_campaign_id directly still
    works identically — they just bypass this helper. Phase 2 will route
    those call sites through this function.
    """
    workspace: dict[str, Any] | None = None
    if workspace_id is not None:
        workspace = get_workspace_by_id(workspace_id)
    else:
        workspace = get_default_workspace()

    if workspace:
        ws_campaign_id = (workspace.get("instantly_campaign_id") or "").strip()
        if ws_campaign_id:
            return ws_campaign_id

    # Env fallback — preserves all existing behavior from Phase 0.
    env_campaign_id = (settings.instantly_campaign_id or "").strip()
    return env_campaign_id or None


def get_campaign_id_source(workspace_id: int | None = None) -> str:
    """Return a human-readable label describing where the campaign ID came from.

    Used by the Settings page to show 'workspace' vs 'env fallback'.
    """
    workspace: dict[str, Any] | None = None
    if workspace_id is not None:
        workspace = get_workspace_by_id(workspace_id)
    else:
        workspace = get_default_workspace()

    if workspace:
        ws_campaign_id = (workspace.get("instantly_campaign_id") or "").strip()
        if ws_campaign_id:
            return "workspace"

    env_campaign_id = (settings.instantly_campaign_id or "").strip()
    if env_campaign_id:
        return "env (INSTANTLY_CAMPAIGN_ID)"

    return "not configured"


# ---------------------------------------------------------------------------
# Phase 2: workspace_id helpers
# ---------------------------------------------------------------------------

def get_default_workspace_id() -> int | None:
    """Return the primary key of the default workspace, or None.

    Thin wrapper around get_default_workspace() for callers that only
    need the id, not the full dict.
    """
    ws = get_default_workspace()
    return ws["id"] if ws else None


def current_workspace_id_or_default() -> int | None:
    """Return the workspace id for the current operating context.

    Phase 2: always returns the default OSP workspace id because workspace
    switching is not yet implemented. Phase 3 will read from a session-level
    context variable set by the workspace selector UI.
    """
    return get_default_workspace_id()


def ensure_workspace_id(value: int | None) -> int | None:
    """Return value if not None, else the default OSP workspace id.

    Used as a safe insertion helper: callers that don't know the workspace
    can pass None and get a sensible default rather than leaving the column
    NULL after Phase 2 migrations run.
    """
    if value is not None:
        return value
    return get_default_workspace_id()


def workspace_column_exists(table_name: str) -> bool:
    """Return True if workspace_id column exists on the given table."""
    try:
        from sqlalchemy import inspect as _inspect
        from src.db import engine as _engine
        inspector = _inspect(_engine)
        tables = set(inspector.get_table_names())
        if table_name not in tables:
            return False
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        return "workspace_id" in cols
    except Exception:
        return False


def backfill_default_workspace_ids() -> dict[str, Any]:
    """Set workspace_id = default OSP id for all NULL rows in workspace-scoped tables.

    Idempotent: the WHERE workspace_id IS NULL clause makes repeated calls
    a no-op for rows already assigned. Tables or columns that don't exist
    are skipped without error.

    Returns a summary dict:
      {
        "default_workspace_id": int | None,
        "leads": <rows_updated>,
        "enrichments": <rows_updated>,
        ...
        "<table>": "skipped_table_not_found" | "skipped_no_column" | "error:..."
      }
    """
    from sqlalchemy import inspect as _inspect, text as _text
    from src.db import engine as _engine

    default_id = get_default_workspace_id()
    results: dict[str, Any] = {"default_workspace_id": default_id}

    if default_id is None:
        log.warning("backfill_workspace_ids_skipped", extra={"reason": "no_default_workspace"})
        results["status"] = "skipped_no_default_workspace"
        return results

    inspector = _inspect(_engine)
    existing_tables = set(inspector.get_table_names())

    for table_name in WORKSPACE_SCOPED_TABLES:
        if table_name not in existing_tables:
            results[table_name] = "skipped_table_not_found"
            continue
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        if "workspace_id" not in cols:
            results[table_name] = "skipped_no_column"
            continue
        try:
            with _engine.begin() as conn:
                result = conn.execute(
                    _text(
                        f"UPDATE {table_name} "
                        "SET workspace_id = :wsid "
                        "WHERE workspace_id IS NULL"
                    ),
                    {"wsid": default_id},
                )
            results[table_name] = result.rowcount
        except Exception as exc:
            log.warning(
                "backfill_workspace_ids_table_failed",
                extra={"table": table_name, "error": f"{type(exc).__name__}: {exc}"},
            )
            results[table_name] = f"error:{type(exc).__name__}"

    log.info("backfill_workspace_ids_complete", extra=results)
    return results


def get_workspace_table_stats() -> dict[str, Any]:
    """Return per-table workspace_id coverage statistics for the Settings diagnostic.

    For each workspace-scoped table, returns:
      - total_rows: int
      - assigned_to_default: int (rows whose workspace_id == default OSP id)
      - missing_workspace_id: int (rows where workspace_id IS NULL)

    Tables that don't exist or don't have the column yet are reported as
    "not_found" or "no_column" respectively.
    """
    from sqlalchemy import inspect as _inspect, text as _text
    from src.db import engine as _engine

    default_id = get_default_workspace_id()
    inspector = _inspect(_engine)
    existing_tables = set(inspector.get_table_names())

    table_stats: dict[str, Any] = {}
    for table_name in WORKSPACE_SCOPED_TABLES:
        if table_name not in existing_tables:
            table_stats[table_name] = {"status": "not_found"}
            continue
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        if "workspace_id" not in cols:
            table_stats[table_name] = {"status": "no_column"}
            continue
        try:
            with _engine.connect() as conn:
                total = conn.execute(
                    _text(f"SELECT COUNT(*) FROM {table_name}")
                ).scalar() or 0
                assigned = (
                    conn.execute(
                        _text(
                            f"SELECT COUNT(*) FROM {table_name} "
                            "WHERE workspace_id = :wsid"
                        ),
                        {"wsid": default_id},
                    ).scalar() or 0
                ) if default_id is not None else 0
                null_count = conn.execute(
                    _text(
                        f"SELECT COUNT(*) FROM {table_name} "
                        "WHERE workspace_id IS NULL"
                    )
                ).scalar() or 0
            table_stats[table_name] = {
                "status": "ok",
                "total_rows": int(total),
                "assigned_to_default": int(assigned),
                "missing_workspace_id": int(null_count),
            }
        except Exception as exc:
            table_stats[table_name] = {"status": f"error:{type(exc).__name__}"}

    return {
        "default_workspace_id": default_id,
        "tables": table_stats,
    }
