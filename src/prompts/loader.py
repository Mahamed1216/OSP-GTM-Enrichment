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


def _db_upsert(
    channel: str,
    content: str,
    *,
    prompt_version: str | None = None,
    prompt_fingerprint: str | None = None,
    updated_by: str | None = None,
) -> None:
    """Insert-or-update the row for `channel`. Always writes a single row
    per channel — the model's unique constraint on `channel` enforces it.

    The audit columns are best-effort: they're set on writes that pass
    a value, and left untouched when the caller doesn't (so callers
    that pre-date the audit fields still work).
    """
    with session_scope() as session:
        existing = session.execute(
            select(PromptConfig).where(PromptConfig.channel == channel)
        ).scalar_one_or_none()
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
                )
            )


def _db_delete(channel: str | None = None) -> None:
    with session_scope() as session:
        if channel is None:
            session.query(PromptConfig).delete()
        else:
            session.query(PromptConfig).filter(PromptConfig.channel == channel).delete()


def get_overlay_metadata(channel: str) -> dict | None:
    """Return DB-side audit metadata for `channel`, or None when the
    row doesn't exist (overlay never saved, falling back to JSON/code).

    Keys: updated_at, prompt_version, prompt_fingerprint, updated_by,
    is_active. The editor renders these in the "Last saved" caption.
    """
    try:
        with session_scope() as session:
            row = session.execute(
                select(PromptConfig).where(PromptConfig.channel == channel)
            ).scalar_one_or_none()
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


def save_overlay(
    channel: str,
    prompt_text: str,
    path: Path = CONFIG_PATH,
    *,
    updated_by: str | None = None,
) -> None:
    """Persist the overlay for one channel to the DB. Other channels untouched.

    For the `email` channel, the text is run through
    `dedupe_email_sections` before the DB write — duplicate H1 headers
    are silently merged. A prior loop revision allowed approval-flow
    appends to accumulate duplicates ("# EXAMPLE — MATCH THIS VOICE
    EXACTLY" was repeating 10 times in the live overlay); deduping at
    the persistence boundary makes that class of bug impossible to
    perpetuate on save.

    `path` is accepted for backwards-compatible signatures but ignored —
    writes always go to the DB so Streamlit Cloud deploys can't wipe
    them. The DB write raises on failure; do NOT swallow it.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    cleaned = prompt_text
    if channel == "email":
        # Lazy import — `cleanup` imports nothing heavy, but keeping the
        # dependency inside the function preserves the loader's "no
        # side-effect at module import" property.
        from src.prompts.cleanup import dedupe_email_sections
        cleaned, dup_stats = dedupe_email_sections(prompt_text)
        if dup_stats:
            log.info(
                "prompt_overlay_deduped_on_save",
                extra={"channel": channel, "duplicates_removed": dup_stats},
            )

    # Best-effort fingerprint + version stamps so the editor can show
    # what's saved and bulk-regen resume can correlate generated rows.
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
        prompt_version=version,
        prompt_fingerprint=fingerprint,
        updated_by=updated_by,
    )


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


def get_effective_prompt_with_source(
    channel: str, default: str, path: Path = CONFIG_PATH
) -> tuple[str, str]:
    """Return (prompt_text, source) where source is one of:
    'database' | 'local_json' | 'code_default'.

    Used by the prompt editor to show "Loaded from: …" so the operator
    can tell whether their last save round-tripped. The same lookup
    order as `get_effective_prompt`; this just preserves the provenance
    for the UI.
    """
    db_val = _db_get(channel)
    if isinstance(db_val, str) and db_val.strip():
        return db_val, "database"
    file_val = _file_load(path).get(channel)
    if isinstance(file_val, str) and file_val.strip():
        return file_val, "local_json"
    return default, "code_default"


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


def clean_saved_overlay(channel: str) -> dict[str, int]:
    """Dedupe-and-resave the live overlay for `channel`. Returns the
    duplicate-removal stats so the caller can show "removed N copies of
    X" in the UI. No-op when no overlay row exists.

    Triggered by the "Clean current saved prompt" button on the editor.
    Idempotent — safe to run repeatedly.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    if channel != "email":
        return {}
    current = _db_get(channel)
    if not isinstance(current, str) or not current.strip():
        return {}
    from src.prompts.cleanup import dedupe_email_sections
    cleaned, stats = dedupe_email_sections(current)
    if cleaned != current:
        _db_upsert(channel, cleaned)
    return stats


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
