"""Tests for scripts/migrate_phase5.py — idempotent schema migration."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import inspect, text

from src.db import engine

_MIGRATE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_phase5.py"


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location("migrate_phase5", _MIGRATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_creates_table_and_column_on_fresh_db():
    """Fresh DB without the new schema → migration adds both."""
    # The autouse fresh_db fixture has already created the full schema with the new
    # model included. Drop the new pieces to simulate pre-migration state.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS content_ratings"))
        # Recreate generated_contents without superseded_by_id column
        conn.execute(text("ALTER TABLE generated_contents RENAME TO _gc_old"))
        conn.execute(text(
            "CREATE TABLE generated_contents AS SELECT "
            "id, lead_id, kind, subject, body, signals_cited, prompt_version, "
            "model, created_at, delivered_at, delivery_provider, delivery_id, "
            "skip_reason FROM _gc_old"
        ))
        conn.execute(text("DROP TABLE _gc_old"))

    inspector = inspect(engine)
    assert "content_ratings" not in inspector.get_table_names()
    cols_before = {c["name"] for c in inspector.get_columns("generated_contents")}
    assert "superseded_by_id" not in cols_before

    module = _load_migrate_module()
    summary = module.migrate(target_engine=engine)
    assert summary["content_ratings_created"] is True
    assert summary["superseded_by_id_added"] is True
    assert summary["already_applied"] is False

    inspector = inspect(engine)
    assert "content_ratings" in inspector.get_table_names()
    cols_after = {c["name"] for c in inspector.get_columns("generated_contents")}
    assert "superseded_by_id" in cols_after


def test_migration_is_idempotent():
    """Running on an already-migrated DB → no-op, no error."""
    module = _load_migrate_module()

    first = module.migrate(target_engine=engine)
    second = module.migrate(target_engine=engine)

    assert second["content_ratings_created"] is False
    assert second["superseded_by_id_added"] is False
    assert second["already_applied"] is True


def test_migration_summary_keys_complete():
    module = _load_migrate_module()
    summary = module.migrate(target_engine=engine)
    assert set(summary.keys()) == {
        "content_ratings_created",
        "superseded_by_id_added",
        "already_applied",
    }
