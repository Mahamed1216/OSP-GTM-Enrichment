"""Phase 2 workspace tests.

Verifies:
  - workspace_id columns exist on all workspace-scoped tables after create_all.
  - backfill_default_workspace_ids() sets workspace_id on NULL rows.
  - Backfill is idempotent: running it twice changes no rows the second time.
  - Rows already assigned to a workspace are left untouched by backfill.
  - New rows created via ORM get default workspace_id from _default_workspace_id().
  - Helper functions work correctly in isolation.
  - Phase 1 behavior is unchanged (default workspace still found, etc.).
"""
from __future__ import annotations

from sqlalchemy import inspect, select, text

from src.db import engine, session_scope
from src.models import (
    GeneratedContent,
    Lead,
    PromptConfig,
    Score,
    Workspace,
)
from src.workspace import (
    WORKSPACE_SCOPED_TABLES,
    backfill_default_workspace_ids,
    current_workspace_id_or_default,
    ensure_workspace_id,
    get_default_workspace_id,
    get_workspace_table_stats,
    seed_default_workspace,
    workspace_column_exists,
)

# conftest.py `fresh_db` fixture drops and recreates all tables before each test.
# Tables are created fresh from Base.metadata.create_all — so workspace_id
# columns defined on the models will be present without needing ALTER TABLE.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_and_get_default_id() -> int:
    """Seed the default workspace and return its id."""
    seed_default_workspace()
    ws_id = get_default_workspace_id()
    assert ws_id is not None, "Default workspace not found after seeding"
    return ws_id


def _make_lead(session, *, suffix: str = "a") -> Lead:
    lead = Lead(
        first_name="Test",
        last_name=f"User{suffix}",
        email=f"test{suffix}@example.com",
        title="VP Sales",
        company="ExampleCo",
        company_domain="example.com",
        industry="B2B SaaS",
    )
    session.add(lead)
    session.flush()
    return lead


# ---------------------------------------------------------------------------
# workspace_id columns present after create_all
# ---------------------------------------------------------------------------

class TestWorkspaceIdColumnsExist:
    """workspace_id must be in every workspace-scoped table after fresh create_all."""

    def test_all_scoped_tables_have_workspace_id(self):
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        for table_name in WORKSPACE_SCOPED_TABLES:
            if table_name not in existing_tables:
                continue  # legacy table absent in test DB — not a failure
            cols = {c["name"] for c in inspector.get_columns(table_name)}
            assert "workspace_id" in cols, (
                f"workspace_id column missing from {table_name}"
            )

    def test_workspace_column_exists_helper(self):
        # At least the main tables should pass.
        for table_name in ("leads", "scores", "generated_contents", "prompt_configs"):
            assert workspace_column_exists(table_name) is True

    def test_workspace_column_exists_returns_false_for_missing_table(self):
        assert workspace_column_exists("table_that_does_not_exist_xyz") is False

    def test_workspaces_table_itself_does_not_have_workspace_id(self):
        # The Workspace table should NOT have a self-referential workspace_id.
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("workspaces")}
        assert "workspace_id" not in cols


# ---------------------------------------------------------------------------
# backfill_default_workspace_ids
# ---------------------------------------------------------------------------

class TestBackfillDefaultWorkspaceIds:
    def test_backfill_returns_no_default_when_unseeded(self):
        result = backfill_default_workspace_ids()
        assert result.get("default_workspace_id") is None
        assert result.get("status") == "skipped_no_default_workspace"

    def test_backfill_after_seeding_sets_null_rows(self):
        # Insert a Lead without workspace_id, then backfill.
        _seed_and_get_default_id()
        with session_scope() as session:
            lead = Lead(
                first_name="A", last_name="B", email="ab@test.com",
                company_domain="test.com",
            )
            session.add(lead)
            session.flush()
            lead_id = lead.id
            # Manually NULL the workspace_id to simulate a pre-Phase-2 row.
            session.execute(
                text("UPDATE leads SET workspace_id = NULL WHERE id = :lid"),
                {"lid": lead_id},
            )

        result = backfill_default_workspace_ids()
        assert result["leads"] >= 1  # at least the row we nulled was backfilled

        default_id = get_default_workspace_id()
        with session_scope() as session:
            ws_id = session.execute(
                select(Lead.workspace_id).where(Lead.id == lead_id)
            ).scalar()
        assert ws_id == default_id

    def test_backfill_is_idempotent(self):
        _seed_and_get_default_id()
        # First backfill: sets all NULLs.
        r1 = backfill_default_workspace_ids()
        # Second backfill: no rows have NULL workspace_id, so rowcount = 0.
        r2 = backfill_default_workspace_ids()
        for table_name in WORKSPACE_SCOPED_TABLES:
            if isinstance(r2.get(table_name), int):
                assert r2[table_name] == 0, (
                    f"Second backfill should update 0 rows in {table_name}, "
                    f"got {r2[table_name]}"
                )

    def test_backfill_does_not_overwrite_existing_workspace_assignment(self):
        """Rows already assigned to a specific workspace_id must not be touched."""
        _seed_and_get_default_id()
        with session_scope() as session:
            # Create a second workspace.
            other_ws = Workspace(
                name="Other", slug="other", is_default=False, is_active=True
            )
            session.add(other_ws)
            session.flush()
            other_id = other_ws.id

            lead = Lead(
                first_name="C", last_name="D", email="cd@test.com",
                company_domain="test.com",
            )
            session.add(lead)
            session.flush()
            # Assign lead to the OTHER workspace explicitly.
            session.execute(
                text("UPDATE leads SET workspace_id = :wsid WHERE id = :lid"),
                {"wsid": other_id, "lid": lead.id},
            )
            lead_id = lead.id

        backfill_default_workspace_ids()

        with session_scope() as session:
            ws_id = session.execute(
                select(Lead.workspace_id).where(Lead.id == lead_id)
            ).scalar()
        assert ws_id == other_id, (
            "Backfill must not overwrite rows already assigned to a workspace"
        )


# ---------------------------------------------------------------------------
# _default_workspace_id / ORM insert-time default
# ---------------------------------------------------------------------------

class TestOrmInsertTimeDefault:
    """Rows inserted via ORM without explicit workspace_id should get the default."""

    def test_lead_gets_default_workspace_id_on_insert(self):
        default_id = _seed_and_get_default_id()
        with session_scope() as session:
            lead = _make_lead(session)
            lead_id = lead.id

        with session_scope() as session:
            row = session.execute(
                select(Lead.workspace_id).where(Lead.id == lead_id)
            ).scalar()
        assert row == default_id

    def test_score_gets_default_workspace_id_on_insert(self):
        default_id = _seed_and_get_default_id()
        with session_scope() as session:
            lead = _make_lead(session, suffix="s")
            score = Score(
                lead_id=lead.id, score=80, tier="B",
                rationale="test", model="test-model",
            )
            session.add(score)
            session.flush()
            score_id = score.id

        with session_scope() as session:
            ws_id = session.execute(
                select(Score.workspace_id).where(Score.id == score_id)
            ).scalar()
        assert ws_id == default_id

    def test_generated_content_gets_default_workspace_id_on_insert(self):
        default_id = _seed_and_get_default_id()
        with session_scope() as session:
            lead = _make_lead(session, suffix="g")
            gc = GeneratedContent(
                lead_id=lead.id, kind="email",
                subject="test", body="test body",
                prompt_version="v1", model="test-model",
                signals_cited=[],
            )
            session.add(gc)
            session.flush()
            gc_id = gc.id

        with session_scope() as session:
            ws_id = session.execute(
                select(GeneratedContent.workspace_id).where(GeneratedContent.id == gc_id)
            ).scalar()
        assert ws_id == default_id

    def test_prompt_config_gets_default_workspace_id_on_insert(self):
        default_id = _seed_and_get_default_id()
        with session_scope() as session:
            pc = PromptConfig(channel="email_test", content="test prompt")
            session.add(pc)
            session.flush()
            pc_id = pc.id

        with session_scope() as session:
            ws_id = session.execute(
                select(PromptConfig.workspace_id).where(PromptConfig.id == pc_id)
            ).scalar()
        assert ws_id == default_id

    def test_insert_returns_none_workspace_id_when_not_seeded(self):
        """When no workspace is seeded, _default_workspace_id() returns None — safe."""
        with session_scope() as session:
            lead = _make_lead(session, suffix="ns")
            # workspace_id should be None (no workspace seeded)
            assert lead.workspace_id is None


# ---------------------------------------------------------------------------
# get_default_workspace_id
# ---------------------------------------------------------------------------

class TestGetDefaultWorkspaceId:
    def test_returns_none_when_no_workspaces(self):
        assert get_default_workspace_id() is None

    def test_returns_int_after_seeding(self):
        seed_default_workspace()
        result = get_default_workspace_id()
        assert isinstance(result, int)
        assert result > 0


# ---------------------------------------------------------------------------
# current_workspace_id_or_default
# ---------------------------------------------------------------------------

class TestCurrentWorkspaceIdOrDefault:
    def test_returns_none_when_no_workspace(self):
        assert current_workspace_id_or_default() is None

    def test_returns_default_id_after_seeding(self):
        seed_default_workspace()
        result = current_workspace_id_or_default()
        expected = get_default_workspace_id()
        assert result == expected


# ---------------------------------------------------------------------------
# ensure_workspace_id
# ---------------------------------------------------------------------------

class TestEnsureWorkspaceId:
    def test_returns_provided_value_unchanged(self):
        assert ensure_workspace_id(42) == 42

    def test_returns_default_when_none_provided(self):
        seed_default_workspace()
        default_id = get_default_workspace_id()
        assert ensure_workspace_id(None) == default_id

    def test_returns_none_when_none_provided_and_no_workspace(self):
        assert ensure_workspace_id(None) is None


# ---------------------------------------------------------------------------
# get_workspace_table_stats
# ---------------------------------------------------------------------------

class TestGetWorkspaceTableStats:
    def test_returns_dict_with_tables_key(self):
        seed_default_workspace()
        stats = get_workspace_table_stats()
        assert "tables" in stats
        assert "default_workspace_id" in stats

    def test_all_scoped_tables_present_in_stats(self):
        seed_default_workspace()
        stats = get_workspace_table_stats()
        tables = stats["tables"]
        for table_name in WORKSPACE_SCOPED_TABLES:
            assert table_name in tables, f"{table_name} missing from stats"

    def test_stats_show_zero_rows_on_empty_tables(self):
        seed_default_workspace()
        stats = get_workspace_table_stats()
        leads_stat = stats["tables"].get("leads", {})
        if leads_stat.get("status") == "ok":
            assert leads_stat["total_rows"] == 0
            assert leads_stat["missing_workspace_id"] == 0

    def test_stats_correctly_count_assigned_rows(self):
        default_id = _seed_and_get_default_id()
        with session_scope() as session:
            _make_lead(session, suffix="s1")
            _make_lead(session, suffix="s2")

        stats = get_workspace_table_stats()
        leads_stat = stats["tables"]["leads"]
        assert leads_stat["status"] == "ok"
        assert leads_stat["total_rows"] == 2
        assert leads_stat["assigned_to_default"] == 2
        assert leads_stat["missing_workspace_id"] == 0
