"""Prompt overlay loader — user-editable overrides for the three
content-generation system prompts (email / linkedin_msg / call_script).

Storage: Supabase Postgres via the `prompt_configs` table (one row per
channel). Previously this lived in ``data/prompts_config.json`` but local
disk on Streamlit Cloud is ephemeral, so every deploy reset the user's
edits. The JSON file is still read as a **deprecated fallback** for local
dev / older checkouts — DB takes precedence whenever a row exists. Writes
go to the DB only.

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
# DB primitives
# ---------------------------------------------------------------------------

def _db_load_all() -> dict[str, str]:
    """Return every channel's stored prompt from the DB.

    Returns an empty dict if the table doesn't exist yet (fresh deploy
    before init_db ran) or the DB is unreachable — callers then fall back
    to the JSON file or the hardcoded defaults.
    """
    try:
        with session_scope() as session:
            rows = session.execute(select(PromptConfig)).scalars().all()
            return {row.channel: row.content for row in rows}
    except Exception as exc:
        log.warning("prompts_db_load_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return {}


def _db_get(channel: str) -> str | None:
    try:
        with session_scope() as session:
            row = session.execute(
                select(PromptConfig).where(PromptConfig.channel == channel)
            ).scalar_one_or_none()
            return row.content if row else None
    except Exception as exc:
        log.warning("prompts_db_get_failed", extra={"channel": channel, "error": f"{type(exc).__name__}: {exc}"})
        return None


def _db_upsert(channel: str, content: str) -> None:
    """Insert-or-update the row for `channel`. Always writes a single row
    per channel — the model's unique constraint on `channel` enforces it."""
    with session_scope() as session:
        existing = session.execute(
            select(PromptConfig).where(PromptConfig.channel == channel)
        ).scalar_one_or_none()
        if existing:
            existing.content = content
            existing.updated_at = datetime.utcnow()
        else:
            session.add(PromptConfig(channel=channel, content=content))


def _db_delete(channel: str | None = None) -> None:
    with session_scope() as session:
        if channel is None:
            session.query(PromptConfig).delete()
        else:
            session.query(PromptConfig).filter(PromptConfig.channel == channel).delete()


def _db_latest_updated_at() -> datetime | None:
    try:
        with session_scope() as session:
            from sqlalchemy import func
            return session.execute(select(func.max(PromptConfig.updated_at))).scalar()
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

def load_overlay(path: Path = CONFIG_PATH) -> dict:
    """Merged view of every channel's stored prompt.

    DB wins per-channel; JSON fills any channel the DB doesn't have a row
    for. `path` is kept for backwards-compatible signatures but only
    affects the JSON fallback.
    """
    file_data = _file_load(path)
    db_data = _db_load_all()
    # DB overrides file on per-channel basis.
    merged = dict(file_data)
    merged.update(db_data)
    return merged


def save_overlay(channel: str, prompt_text: str, path: Path = CONFIG_PATH) -> None:
    """Persist the overlay for one channel to the DB. Other channels untouched.

    `path` is accepted for backwards-compatible signatures but ignored —
    writes always go to the DB so Streamlit Cloud deploys can't wipe them.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    _db_upsert(channel, prompt_text)


def get_effective_prompt(channel: str, default: str, path: Path = CONFIG_PATH) -> str:
    """Return the override text for `channel`, falling back to `default`.

    Lookup order: DB row → JSON file → hardcoded default. The JSON tier is
    a transitional fallback for local dev / pre-migration checkouts; new
    edits never write there.
    """
    db_val = _db_get(channel)
    if isinstance(db_val, str) and db_val.strip():
        return db_val
    file_val = _file_load(path).get(channel)
    if isinstance(file_val, str) and file_val.strip():
        return file_val
    return default


def reset_overlay(channel: str, path: Path = CONFIG_PATH) -> None:
    """Drop one channel's DB override so future generations use the default.

    Does NOT touch the JSON fallback file — if a stale JSON value exists
    it will surface again, which matches the documented order (DB → JSON →
    default). Delete data/prompts_config.json by hand if you want a true
    full reset.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    _db_delete(channel)


def reset_all_overlays(path: Path = CONFIG_PATH) -> None:
    """Drop every channel's DB override."""
    _db_delete(None)


def get_last_saved_timestamp(path: Path = CONFIG_PATH) -> str | None:
    """Most recent `updated_at` across DB rows, or JSON file mtime as
    a fallback. Returns None if neither source has data."""
    ts = _db_latest_updated_at()
    if ts is not None:
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    if path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return None
