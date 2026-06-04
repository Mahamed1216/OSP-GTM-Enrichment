"""Tests for workspace-scoped ICP/company settings (Phase 4 hotfix).

Verifies:
  1. Save company settings for OSP.
  2. Create Test Client workspace.
  3. Save different settings for Test Client.
  4. OSP settings are unchanged after saving Test Client settings.
  5. Test Client settings are independent from OSP.
  6. Editing Test Client settings does not update OSP rows.
  7. Editing OSP settings does not update Test Client rows.
  8. Prompt configs remain workspace-scoped and unchanged.
  9. Existing OSP settings are backfilled from the JSON file on startup.
  10. New workspace with no saved settings returns defaults (not OSP values).
"""
from __future__ import annotations

import pytest

from src.db import session_scope
from src.icp_config import (
    ICPConfig,
    CompanyProfile,
    copy_workspace_icp_config,
    default_icp_config,
    load_workspace_icp_config,
    save_workspace_icp_config,
)
from src.models import Workspace
from src.workspace import (
    backfill_osp_icp_config,
    create_workspace,
    get_default_workspace_id,
    seed_default_workspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_osp() -> int:
    seed_default_workspace()
    osp_id = get_default_workspace_id()
    assert osp_id is not None
    return osp_id


def _make_osp_cfg(name: str = "OSP Agency", one_liner: str = "OSP outbound") -> ICPConfig:
    cfg = default_icp_config()
    cfg.company = CompanyProfile(
        name=name,
        one_liner=one_liner,
        value_props=["OSP value prop 1"],
        differentiators=["OSP differentiator"],
    )
    return cfg


def _make_client_cfg(name: str = "Client A", one_liner: str = "B2B Tech Recruitment") -> ICPConfig:
    cfg = ICPConfig()  # blank
    cfg.company = CompanyProfile(
        name=name,
        one_liner=one_liner,
        value_props=["Client value prop"],
        differentiators=["Client differentiator"],
    )
    return cfg


# ---------------------------------------------------------------------------
# 1 & 2. Save and load OSP settings
# ---------------------------------------------------------------------------

class TestOspSettingsPersist:
    def test_save_and_load_osp_settings(self):
        osp_id = _seed_osp()
        cfg = _make_osp_cfg()
        save_workspace_icp_config(cfg, workspace_id=osp_id)

        loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert loaded.company.name == "OSP Agency"
        assert loaded.company.one_liner == "OSP outbound"

    def test_save_one_liner_roundtrips(self):
        osp_id = _seed_osp()
        cfg = _make_osp_cfg(one_liner="The best outbound agency")
        save_workspace_icp_config(cfg, workspace_id=osp_id)

        loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert loaded.company.one_liner == "The best outbound agency"


# ---------------------------------------------------------------------------
# 3–5. Create client workspace and verify isolation
# ---------------------------------------------------------------------------

class TestClientWorkspaceIsolation:
    def test_new_workspace_gets_blank_settings(self):
        _seed_osp()
        new_ws = create_workspace(
            name="Blank Settings WS",
            slug="blank-settings-ws",
            instantly_campaign_id="camp-blank",
        )
        loaded = load_workspace_icp_config(workspace_id=new_ws["id"])
        # New workspace with no settings → code defaults (not OSP values)
        assert loaded == default_icp_config()

    def test_client_settings_do_not_affect_osp(self):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("OSP Agency"), workspace_id=osp_id)

        new_ws = create_workspace(
            name="Client B",
            slug="client-b",
            instantly_campaign_id="camp-client-b",
        )
        client_cfg = _make_client_cfg("Client B Corp", "B2B Recruitment")
        save_workspace_icp_config(client_cfg, workspace_id=new_ws["id"])

        osp_loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert osp_loaded.company.name == "OSP Agency"
        assert osp_loaded.company.one_liner == "OSP outbound"

    def test_osp_settings_do_not_affect_client(self):
        osp_id = _seed_osp()
        new_ws = create_workspace(
            name="Client C",
            slug="client-c",
            instantly_campaign_id="camp-client-c",
        )
        save_workspace_icp_config(_make_client_cfg("Client C Corp"), workspace_id=new_ws["id"])

        # Now edit OSP settings
        save_workspace_icp_config(_make_osp_cfg("OSP New Name"), workspace_id=osp_id)

        client_loaded = load_workspace_icp_config(workspace_id=new_ws["id"])
        assert client_loaded.company.name == "Client C Corp"

    def test_two_clients_are_independent(self):
        _seed_osp()
        ws1 = create_workspace(name="Client D", slug="client-d", instantly_campaign_id="c-d")
        ws2 = create_workspace(name="Client E", slug="client-e", instantly_campaign_id="c-e")

        save_workspace_icp_config(_make_client_cfg("Client D Corp"), workspace_id=ws1["id"])
        save_workspace_icp_config(_make_client_cfg("Client E Corp"), workspace_id=ws2["id"])

        loaded1 = load_workspace_icp_config(workspace_id=ws1["id"])
        loaded2 = load_workspace_icp_config(workspace_id=ws2["id"])
        assert loaded1.company.name == "Client D Corp"
        assert loaded2.company.name == "Client E Corp"


# ---------------------------------------------------------------------------
# 6–7. Editing one workspace does not change the other's DB row
# ---------------------------------------------------------------------------

class TestDbRowIsolation:
    def test_edit_client_does_not_touch_osp_db_row(self):
        from sqlalchemy import select
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("OSP Real"), workspace_id=osp_id)

        new_ws = create_workspace(name="Client F", slug="client-f", instantly_campaign_id="c-f")
        save_workspace_icp_config(_make_client_cfg("Client F Corp"), workspace_id=new_ws["id"])

        with session_scope() as session:
            osp_ws = session.get(Workspace, osp_id)
            assert osp_ws is not None
            assert osp_ws.icp_config is not None
            assert osp_ws.icp_config["company"]["name"] == "OSP Real"

    def test_edit_osp_does_not_touch_client_db_row(self):
        osp_id = _seed_osp()
        new_ws = create_workspace(name="Client G", slug="client-g", instantly_campaign_id="c-g")
        save_workspace_icp_config(_make_client_cfg("Client G Corp"), workspace_id=new_ws["id"])

        save_workspace_icp_config(_make_osp_cfg("OSP Updated"), workspace_id=osp_id)

        with session_scope() as session:
            client_ws = session.get(Workspace, new_ws["id"])
            assert client_ws is not None
            assert client_ws.icp_config is not None
            assert client_ws.icp_config["company"]["name"] == "Client G Corp"


# ---------------------------------------------------------------------------
# 8. Prompt configs are not affected by settings changes
# ---------------------------------------------------------------------------

class TestPromptsUnaffectedBySettings:
    def test_saving_settings_does_not_change_prompt_configs(self):
        from src.models import PromptConfig
        from sqlalchemy import select
        osp_id = _seed_osp()

        with session_scope() as session:
            session.add(PromptConfig(
                channel="email", content="OSP email prompt",
                is_active=True, workspace_id=osp_id,
            ))

        save_workspace_icp_config(_make_osp_cfg(), workspace_id=osp_id)

        with session_scope() as session:
            row = session.execute(
                select(PromptConfig)
                .where(PromptConfig.workspace_id == osp_id)
                .where(PromptConfig.channel == "email")
            ).scalar_one_or_none()
            assert row is not None
            assert row.content == "OSP email prompt"


# ---------------------------------------------------------------------------
# 9. Backfill from file preserves OSP settings
# ---------------------------------------------------------------------------

class TestBackfillOspIcpConfig:
    def test_backfill_reads_from_file_when_db_column_null(self, tmp_path):
        import json
        osp_id = _seed_osp()

        fake_cfg = default_icp_config()
        fake_cfg.company.name = "Backfill OSP Name"
        config_file = tmp_path / "icp_config.json"
        config_file.write_text(fake_cfg.model_dump_json(indent=2), encoding="utf-8")

        # Patch CONFIG_PATH to point to our tmp file.
        import src.icp_config as _icp_mod
        original = _icp_mod.CONFIG_PATH
        _icp_mod.CONFIG_PATH = config_file
        try:
            result = backfill_osp_icp_config()
            assert result is True
            loaded = load_workspace_icp_config(workspace_id=osp_id)
            assert loaded.company.name == "Backfill OSP Name"
        finally:
            _icp_mod.CONFIG_PATH = original

    def test_backfill_is_no_op_when_already_set(self):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("Already Set"), workspace_id=osp_id)
        result = backfill_osp_icp_config()
        assert result is False  # skipped because column is already populated

        # Value should remain unchanged
        loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert loaded.company.name == "Already Set"


# ---------------------------------------------------------------------------
# 10. New workspace returns defaults (not OSP values) when no settings saved
# ---------------------------------------------------------------------------

class TestNewWorkspaceDefaultSettings:
    def test_new_workspace_settings_are_code_defaults(self):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("OSP Company"), workspace_id=osp_id)

        new_ws = create_workspace(name="Fresh WS", slug="fresh-ws", instantly_campaign_id="c-fresh")
        loaded = load_workspace_icp_config(workspace_id=new_ws["id"])

        # Should NOT inherit OSP's "OSP Company" name
        assert loaded.company.name != "OSP Company"

    def test_copy_settings_from_workspace_works(self):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("To Be Copied"), workspace_id=osp_id)

        new_ws = create_workspace(
            name="Copy Test WS",
            slug="copy-test-ws",
            instantly_campaign_id="c-copy",
            copy_settings_from_workspace_id=osp_id,
        )
        loaded = load_workspace_icp_config(workspace_id=new_ws["id"])
        assert loaded.company.name == "To Be Copied"

    def test_copy_settings_checkbox_false_gives_blank(self):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("OSP Name"), workspace_id=osp_id)

        new_ws = create_workspace(
            name="No Copy WS",
            slug="no-copy-ws",
            instantly_campaign_id="c-nocopy",
            copy_settings_from_workspace_id=None,
        )
        loaded = load_workspace_icp_config(workspace_id=new_ws["id"])
        assert loaded.company.name != "OSP Name"


# ---------------------------------------------------------------------------
# copy_workspace_icp_config utility
# ---------------------------------------------------------------------------

class TestCopyWorkspaceIcpConfig:
    def test_copy_icp_config_between_workspaces(self):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("Source WS"), workspace_id=osp_id)

        ws2 = create_workspace(name="Dest WS", slug="dest-ws", instantly_campaign_id="c-dest")
        result = copy_workspace_icp_config(osp_id, ws2["id"])
        assert result is True

        loaded = load_workspace_icp_config(workspace_id=ws2["id"])
        assert loaded.company.name == "Source WS"

    def test_copy_is_independent_after_copy(self):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("Original"), workspace_id=osp_id)

        ws2 = create_workspace(name="After Copy WS", slug="after-copy-ws", instantly_campaign_id="c-ac")
        copy_workspace_icp_config(osp_id, ws2["id"])

        # Editing OSP after copy should not affect ws2
        save_workspace_icp_config(_make_osp_cfg("Changed After Copy"), workspace_id=osp_id)
        loaded_ws2 = load_workspace_icp_config(workspace_id=ws2["id"])
        assert loaded_ws2.company.name == "Original"
