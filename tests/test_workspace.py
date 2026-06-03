"""Phase 1 workspace tests.

Verifies:
  - workspaces table is created by Base.metadata.create_all (via conftest fresh_db).
  - seed_default_workspace creates the OSP workspace on first call.
  - Re-running seed_default_workspace is a no-op (idempotent).
  - get_default_workspace returns the OSP workspace.
  - get_or_create_default_workspace works from a blank slate.
  - get_workspace_by_id returns None for missing IDs and the dict for valid ones.
  - get_active_workspaces returns the seeded workspace.
  - get_campaign_id_for_workspace:
      - returns workspace.instantly_campaign_id when set.
      - falls back to settings.instantly_campaign_id when workspace value is blank.
  - get_campaign_id_source returns correct labels.
"""
import os
from unittest.mock import patch

import pytest
from sqlalchemy import select

from src.db import session_scope
from src.models import Workspace
from src.workspace import (
    get_active_workspaces,
    get_campaign_id_for_workspace,
    get_campaign_id_source,
    get_default_workspace,
    get_or_create_default_workspace,
    get_workspace_by_id,
    seed_default_workspace,
)

# conftest.py provides the `fresh_db` autouse fixture that drops and recreates
# all tables before each test, so every test here starts with an empty workspaces
# table.


class TestSeedDefaultWorkspace:
    def test_creates_osp_workspace_when_table_empty(self):
        seed_default_workspace()
        with session_scope() as session:
            count = len(session.execute(select(Workspace)).scalars().all())
        assert count == 1

    def test_workspace_has_correct_defaults(self):
        seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
        assert ws.name == "OSP"
        assert ws.slug == "osp"
        assert ws.is_default is True
        assert ws.is_active is True

    def test_idempotent_no_duplicates(self):
        seed_default_workspace()
        seed_default_workspace()  # second call is a no-op
        seed_default_workspace()  # third call is a no-op
        with session_scope() as session:
            count = len(session.execute(select(Workspace)).scalars().all())
        assert count == 1

    def test_uses_env_campaign_id_when_set(self):
        with patch("src.workspace.settings") as mock_settings:
            mock_settings.instantly_campaign_id = "test-campaign-abc"
            seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
        assert ws.instantly_campaign_id == "test-campaign-abc"

    def test_campaign_id_null_when_env_blank(self):
        with patch("src.workspace.settings") as mock_settings:
            mock_settings.instantly_campaign_id = ""
            seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
        assert ws.instantly_campaign_id is None


class TestGetDefaultWorkspace:
    def test_returns_none_when_no_workspaces(self):
        # fresh_db fixture — no workspaces yet
        result = get_default_workspace()
        assert result is None

    def test_returns_dict_after_seeding(self):
        seed_default_workspace()
        ws = get_default_workspace()
        assert ws is not None
        assert ws["name"] == "OSP"
        assert ws["slug"] == "osp"
        assert ws["is_default"] is True

    def test_returns_dict_not_orm_object(self):
        seed_default_workspace()
        ws = get_default_workspace()
        assert isinstance(ws, dict)


class TestGetOrCreateDefaultWorkspace:
    def test_seeds_and_returns_from_blank_db(self):
        ws = get_or_create_default_workspace()
        assert ws["name"] == "OSP"

    def test_does_not_duplicate_when_already_exists(self):
        seed_default_workspace()
        ws = get_or_create_default_workspace()
        assert ws["name"] == "OSP"
        with session_scope() as session:
            count = len(session.execute(select(Workspace)).scalars().all())
        assert count == 1


class TestGetWorkspaceById:
    def test_returns_none_for_missing_id(self):
        result = get_workspace_by_id(9999)
        assert result is None

    def test_returns_workspace_for_valid_id(self):
        seed_default_workspace()
        ws = get_default_workspace()
        assert ws is not None
        result = get_workspace_by_id(ws["id"])
        assert result is not None
        assert result["slug"] == "osp"


class TestGetActiveWorkspaces:
    def test_returns_empty_list_when_no_workspaces(self):
        result = get_active_workspaces()
        assert result == []

    def test_returns_seeded_workspace(self):
        seed_default_workspace()
        result = get_active_workspaces()
        assert len(result) == 1
        assert result[0]["slug"] == "osp"

    def test_excludes_inactive_workspaces(self):
        seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
            ws.is_active = False
        result = get_active_workspaces()
        assert result == []


class TestGetCampaignIdForWorkspace:
    def test_returns_workspace_campaign_id_when_set(self):
        seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
            ws.instantly_campaign_id = "workspace-campaign-123"

        result = get_campaign_id_for_workspace()
        assert result == "workspace-campaign-123"

    def test_falls_back_to_env_when_workspace_value_blank(self):
        seed_default_workspace()
        # Workspace campaign ID is blank (set by seed when env was "")
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
            ws.instantly_campaign_id = None

        with patch("src.workspace.settings") as mock_settings:
            mock_settings.instantly_campaign_id = "env-campaign-xyz"
            result = get_campaign_id_for_workspace()

        assert result == "env-campaign-xyz"

    def test_returns_none_when_neither_workspace_nor_env(self):
        seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
            ws.instantly_campaign_id = None

        with patch("src.workspace.settings") as mock_settings:
            mock_settings.instantly_campaign_id = ""
            result = get_campaign_id_for_workspace()

        assert result is None

    def test_workspace_id_lookup_by_explicit_id(self):
        seed_default_workspace()
        ws = get_default_workspace()
        assert ws is not None
        with session_scope() as session:
            row = session.execute(select(Workspace)).scalar_one()
            row.instantly_campaign_id = "explicit-id-test"

        result = get_campaign_id_for_workspace(workspace_id=ws["id"])
        assert result == "explicit-id-test"


class TestGetCampaignIdSource:
    def test_workspace_source_when_workspace_has_campaign_id(self):
        seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
            ws.instantly_campaign_id = "ws-campaign"
        assert get_campaign_id_source() == "workspace"

    def test_env_source_when_workspace_blank(self):
        seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
            ws.instantly_campaign_id = None

        with patch("src.workspace.settings") as mock_settings:
            mock_settings.instantly_campaign_id = "env-id"
            source = get_campaign_id_source()

        assert source == "env (INSTANTLY_CAMPAIGN_ID)"

    def test_not_configured_when_both_blank(self):
        seed_default_workspace()
        with session_scope() as session:
            ws = session.execute(select(Workspace)).scalar_one()
            ws.instantly_campaign_id = None

        with patch("src.workspace.settings") as mock_settings:
            mock_settings.instantly_campaign_id = ""
            source = get_campaign_id_source()

        assert source == "not configured"
