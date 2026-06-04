"""Prompt overlay loader — user-editable overrides for the three
content-generation system prompts (email / linkedin_msg / call_script).

Storage: Supabase Postgres via the `prompt_configs` table (one row per
(channel, workspace_id) pair). Previously this lived in
``data/prompts_config.json`` but local disk on Streamlit Cloud is ephemeral,
so every deploy reset the user's edits. The JSON file is still read as a
**deprecated fallback** for local dev / older checkouts — DB takes precedence
whenever a row exists. Writes go to the DB only.

Phase 4: all DB functions now accept workspace_id so each workspace has
independent prompts. When workspace_id=None, behaviour falls back to the
default (OSP) workspace for backward compatibility with content generators
and the self-improvement loop.

Public API (unchanged so callers in src/prompts/{email,linkedin_msg,
call_script}.py and app/pages/7_prompts.py don't need to update):

    load_overlay()                     -> dict[channel, content]
    save_overlay(channel, prompt_text) -> None       (DB upsert)
    get_effective_prompt(channel, default) -> str
    reset_overlay(channel)             -> None       (deletes DB row)
    reset_all_overlays()               -> None       (deletes all rows)
    get_last_saved_timestamp()         -> str | None
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from src.db import session_scope
from src.models import PromptConfig

log = logging.getLogger(__name__)

# Deprecated local-dev fallback. Still read so an existing JSON checkout
# keeps working, but never written.
CONFIG_PATH = Path("data/prompts_config.json")

_VALID_CHANNELS = {"email", "linkedin_msg", "call_script"}


# ---------------------------------------------------------------------------
# Workspace-id resolver (lazy import to avoid circular deps)
# ---------------------------------------------------------------------------

def _resolve_workspace_id(workspace_id: int | None) -> int | None:
    """Return workspace_id, defaulting to the default workspace when None."""
    if workspace_id is not None:
        return workspace_id
    try:
        from src.workspace import get_default_workspace_id
        return get_default_workspace_id()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DB primitives
# ---------------------------------------------------------------------------

def _db_load_all(workspace_id: int | None = None) -> dict[str, str]:
    """Return every channel's stored prompt for a workspace from the DB.

    When workspace_id is None, uses the default workspace (backward compat).
    Returns an empty dict if the table doesn't exist yet or the DB is
    unreachable — callers then fall back to the JSON file or code defaults.
    """
    try:
        ws_id = _resolve_workspace_id(workspace_id)
        with session_scope() as session:
            stmt = select(PromptConfig)
            if ws_id is not None:
                stmt = stmt.where(PromptConfig.workspace_id == ws_id)
            rows = session.execute(stmt).scalars().all()
            return {row.channel: row.content for row in rows}
    except Exception as exc:
        log.warning("prompts_db_load_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return {}


def _db_get(channel: str, workspace_id: int | None = None) -> str | None:
    """Return the prompt content for a channel+workspace, or None."""
    try:
        ws_id = _resolve_workspace_id(workspace_id)
        with session_scope() as session:
            stmt = select(PromptConfig).where(PromptConfig.channel == channel)
            if ws_id is not None:
                stmt = stmt.where(PromptConfig.workspace_id == ws_id)
            else:
                stmt = stmt.limit(1)
            row = session.execute(stmt).scalar_one_or_none()
            return row.content if row else None
    except Exception as exc:
        log.warning("prompts_db_get_failed", extra={"channel": channel, "error": f"{type(exc).__name__}: {exc}"})
        return None


def _db_upsert(
    channel: str,
    content: str,
    *,
    workspace_id: int | None = None,
    prompt_version: str | None = None,
    prompt_fingerprint: str | None = None,
    updated_by: str | None = None,
) -> None:
    """Insert-or-update the row for (channel, workspace_id).

    When workspace_id is None, resolves to the default workspace so
    backward-compat callers (self-improvement loop, content generators)
    always write to the OSP workspace.
    """
    ws_id = _resolve_workspace_id(workspace_id)
    with session_scope() as session:
        stmt = select(PromptConfig).where(PromptConfig.channel == channel)
        if ws_id is not None:
            stmt = stmt.where(PromptConfig.workspace_id == ws_id)
        else:
            stmt = stmt.limit(1)
        existing = session.execute(stmt).scalar_one_or_none()
        if existing:
            existing.content = content
            existing.updated_at = datetime.utcnow()
            if prompt_version is not None:
                existing.prompt_version = prompt_version
            if prompt_fingerprint is not None:
                existing.prompt_fingerprint = prompt_fingerprint
            if updated_by is not None:
                existing.updated_by = updated_by
            existing.is_active = True
        else:
            session.add(
                PromptConfig(
                    channel=channel,
                    content=content,
                    prompt_version=prompt_version,
                    prompt_fingerprint=prompt_fingerprint,
                    updated_by=updated_by,
                    is_active=True,
                    workspace_id=ws_id,
                )
            )


def _db_delete(channel: str | None = None, workspace_id: int | None = None) -> None:
    """Delete prompt rows. workspace_id=None means delete across all workspaces."""
    with session_scope() as session:
        q = session.query(PromptConfig)
        if workspace_id is not None:
            q = q.filter(PromptConfig.workspace_id == workspace_id)
        if channel is not None:
            q = q.filter(PromptConfig.channel == channel)
        q.delete()


def get_overlay_metadata(channel: str, workspace_id: int | None = None) -> dict | None:
    """Return DB-side audit metadata for (channel, workspace_id), or None.

    Keys: updated_at, prompt_version, prompt_fingerprint, updated_by,
    is_active. The editor renders these in the "Last saved" caption.
    """
    try:
        ws_id = _resolve_workspace_id(workspace_id)
        with session_scope() as session:
            stmt = select(PromptConfig).where(PromptConfig.channel == channel)
            if ws_id is not None:
                stmt = stmt.where(PromptConfig.workspace_id == ws_id)
            else:
                stmt = stmt.limit(1)
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return None
            return {
                "updated_at": row.updated_at,
                "prompt_version": row.prompt_version,
                "prompt_fingerprint": row.prompt_fingerprint,
                "updated_by": row.updated_by,
                "is_active": bool(row.is_active),
            }
    except Exception as exc:
        log.warning(
            "prompts_db_metadata_failed",
            extra={"channel": channel, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


def _db_latest_updated_at(workspace_id: int | None = None) -> datetime | None:
    try:
        ws_id = _resolve_workspace_id(workspace_id)
        with session_scope() as session:
            from sqlalchemy import func
            stmt = select(func.max(PromptConfig.updated_at))
            if ws_id is not None:
                stmt = stmt.where(PromptConfig.workspace_id == ws_id)
            return session.execute(stmt).scalar()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# JSON fallback (deprecated — read-only)
# ---------------------------------------------------------------------------

def _file_load(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log.warning("prompts_overlay_not_a_dict", extra={"path": str(path)})
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "prompts_overlay_load_failed",
            extra={"path": str(path), "error": str(exc)},
        )
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_overlay(path: Path = CONFIG_PATH, *, workspace_id: int | None = None) -> dict:
    """Merged view of every channel's stored prompt for the given workspace.

    DB wins per-channel; JSON fills any channel the DB doesn't have a row
    for. `path` is kept for backwards-compatible signatures but only
    affects the JSON fallback. When workspace_id=None, uses the default
    workspace (backward compat).
    """
    file_data = _file_load(path)
    db_data = _db_load_all(workspace_id)
    merged = dict(file_data)
    merged.update(db_data)
    return merged


def save_overlay(
    channel: str,
    prompt_text: str,
    path: Path = CONFIG_PATH,
    *,
    workspace_id: int | None = None,
    updated_by: str | None = None,
) -> None:
    """Persist the overlay for one (channel, workspace) pair to the DB.

    When workspace_id=None, resolves to the default workspace so existing
    callers (self-improvement loop, rollback) continue working unchanged.
    Writes always go to the DB — `path` is accepted for backward-compatible
    signatures only.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    cleaned = prompt_text
    if channel == "email":
        from src.prompts.cleanup import dedupe_email_sections
        cleaned, dup_stats = dedupe_email_sections(prompt_text)
        if dup_stats:
            log.info(
                "prompt_overlay_deduped_on_save",
                extra={"channel": channel, "duplicates_removed": dup_stats},
            )

    import hashlib
    fingerprint = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
    version: str | None = None
    if channel == "email":
        try:
            from src.prompts.email import PROMPT_VERSION
            version = PROMPT_VERSION
        except Exception:
            version = None

    _db_upsert(
        channel, cleaned,
        workspace_id=workspace_id,
        prompt_version=version,
        prompt_fingerprint=fingerprint,
        updated_by=updated_by,
    )


def get_effective_prompt(
    channel: str,
    default: str,
    path: Path = CONFIG_PATH,
    *,
    workspace_id: int | None = None,
) -> str:
    """Return the override text for (channel, workspace), falling back to `default`.

    Lookup order: DB row → JSON file → hardcoded default. The JSON tier is
    a transitional fallback for local dev / pre-migration checkouts; new
    edits never write there. When workspace_id=None, uses the default
    workspace (backward compat for content generators).
    """
    db_val = _db_get(channel, workspace_id)
    if isinstance(db_val, str) and db_val.strip():
        return db_val
    file_val = _file_load(path).get(channel)
    if isinstance(file_val, str) and file_val.strip():
        return file_val
    return default


def get_effective_prompt_with_source(
    channel: str,
    default: str,
    path: Path = CONFIG_PATH,
    *,
    workspace_id: int | None = None,
) -> tuple[str, str]:
    """Return (prompt_text, source) where source is one of:
    'database' | 'local_json' | 'code_default'.

    Used by the prompt editor to show "Loaded from: …" so the operator
    can tell whether their last save round-tripped. When workspace_id=None,
    uses the default workspace (backward compat).
    """
    db_val = _db_get(channel, workspace_id)
    if isinstance(db_val, str) and db_val.strip():
        return db_val, "database"
    file_val = _file_load(path).get(channel)
    if isinstance(file_val, str) and file_val.strip():
        return file_val, "local_json"
    return default, "code_default"


def reset_overlay(
    channel: str,
    path: Path = CONFIG_PATH,
    *,
    workspace_id: int | None = None,
) -> None:
    """Drop one channel's DB row for the given workspace.

    When workspace_id=None, resolves to the default workspace. Does NOT
    touch the JSON fallback file.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    ws_id = _resolve_workspace_id(workspace_id)
    _db_delete(channel, ws_id)


def reset_all_overlays(
    path: Path = CONFIG_PATH,
    *,
    workspace_id: int | None = None,
) -> None:
    """Drop all channel rows for the given workspace (or all workspaces when
    workspace_id=None — legacy behaviour used in tests/scripts)."""
    _db_delete(None, workspace_id)


def clean_saved_overlay(channel: str, *, workspace_id: int | None = None) -> dict[str, int]:
    """Dedupe-and-resave the live overlay for (channel, workspace). Returns
    the duplicate-removal stats. No-op when no overlay row exists.

    Triggered by the "Clean current saved prompt" button on the editor.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    if channel != "email":
        return {}
    current = _db_get(channel, workspace_id)
    if not isinstance(current, str) or not current.strip():
        return {}
    from src.prompts.cleanup import dedupe_email_sections
    cleaned, stats = dedupe_email_sections(current)
    if cleaned != current:
        _db_upsert(channel, cleaned, workspace_id=workspace_id)
    return stats


def get_last_saved_timestamp(
    path: Path = CONFIG_PATH,
    *,
    workspace_id: int | None = None,
) -> str | None:
    """Most recent `updated_at` for the given workspace's DB rows, or JSON
    file mtime as a fallback. Returns None if neither source has data."""
    ts = _db_latest_updated_at(workspace_id)
    if ts is not None:
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    if path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return None
