"""Tests for the reply agent schema migration safety (hotfix).

Verifies that:
1. A fresh database creates reply_threads during init_db.
2. Calling init_db / ensure_tables repeatedly does not crash or duplicate.
3. The reply queue returns an empty list when the table exists but has no rows.
4. ensure_reply_agent_schema handles a genuinely missing table gracefully.
5. ensure_tables is idempotent on an already-correct schema.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from src.db import Base, engine, ensure_tables, init_db, session_scope
from src.feedback.reply_sync import (
    ensure_reply_agent_schema,
    get_reply_queue,
)
from src.workspace import get_default_workspace_id, seed_default_workspace


# ---------------------------------------------------------------------------
# 1. Fresh database creates reply_threads
# ---------------------------------------------------------------------------

def test_fresh_db_creates_reply_threads():
    """After fresh_db fixture runs Base.metadata.create_all, reply_threads must exist."""
    tables = set(inspect(engine).get_table_names())
    assert "reply_threads" in tables, "reply_threads table must exist after schema creation"
    assert "reply_drafts" in tables, "reply_drafts table must exist after schema creation"


# ---------------------------------------------------------------------------
# 2. Re-running init_db / ensure_tables does not crash
# ---------------------------------------------------------------------------

def test_ensure_tables_idempotent():
    """Calling ensure_tables when tables already exist is a no-op and does not error."""
    result1 = ensure_tables("reply_threads", "reply_drafts")
    result2 = ensure_tables("reply_threads", "reply_drafts")
    assert result1 is True
    assert result2 is True


def test_ensure_tables_with_nonexistent_table_name_ignored():
    """Requesting a table name not in Base.metadata is silently ignored."""
    result = ensure_tables("reply_threads", "nonexistent_fantasy_table_xyz")
    assert result is True, "ensure_tables must succeed even for unknown table names"


def test_ensure_tables_creates_missing_table(monkeypatch):
    """ensure_tables creates a table that was dropped from the live DB."""
    # Drop reply_threads to simulate a missing table
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS reply_threads"))

    tables_before = set(inspect(engine).get_table_names())
    assert "reply_threads" not in tables_before

    result = ensure_tables("reply_threads")
    assert result is True

    tables_after = set(inspect(engine).get_table_names())
    assert "reply_threads" in tables_after, "ensure_tables must recreate the dropped table"


# ---------------------------------------------------------------------------
# 3. Reply queue returns empty list when table exists but has no rows
# ---------------------------------------------------------------------------

def test_reply_queue_empty_on_fresh_db():
    """get_reply_queue returns [] when table exists but has no data."""
    seed_default_workspace()
    ws_id = get_default_workspace_id()
    threads = get_reply_queue(workspace_id=ws_id)
    assert threads == [], "get_reply_queue must return empty list on a fresh DB"


# ---------------------------------------------------------------------------
# 4. ensure_reply_agent_schema handles missing table gracefully
# ---------------------------------------------------------------------------

def test_ensure_reply_agent_schema_with_missing_table():
    """ensure_reply_agent_schema recreates reply_threads if it was dropped."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS reply_threads"))

    tables_before = set(inspect(engine).get_table_names())
    assert "reply_threads" not in tables_before

    result = ensure_reply_agent_schema()
    assert result is True, "ensure_reply_agent_schema must succeed even after table is dropped"

    tables_after = set(inspect(engine).get_table_names())
    assert "reply_threads" in tables_after


def test_ensure_reply_agent_schema_succeeds_on_intact_db():
    """ensure_reply_agent_schema returns True when tables already exist."""
    result = ensure_reply_agent_schema()
    assert result is True


# ---------------------------------------------------------------------------
# 5. reply_threads table has all required columns
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS = {
    "id", "workspace_id", "lead_id", "campaign_id", "instantly_lead_id",
    "prospect_email", "prospect_name", "company_name", "thread_id", "message_id",
    "inbound_reply_text", "original_outbound_email", "reply_received_at",
    "classification", "recommended_action", "draft_body", "human_review_notes",
    "status", "sent_at", "send_error", "raw_payload", "dedup_key",
    "created_at", "updated_at",
}


def test_reply_threads_has_all_required_columns():
    """reply_threads must have every column required by the Reply Queue UI."""
    cols = {c["name"] for c in inspect(engine).get_columns("reply_threads")}
    missing = _REQUIRED_COLUMNS - cols
    assert not missing, f"reply_threads is missing columns: {missing}"


def test_reply_drafts_has_required_columns():
    required = {"id", "workspace_id", "inbound_reply", "classification",
                "recommended_action", "draft_body", "human_review_notes",
                "created_at", "updated_at"}
    cols = {c["name"] for c in inspect(engine).get_columns("reply_drafts")}
    missing = required - cols
    assert not missing, f"reply_drafts is missing columns: {missing}"
