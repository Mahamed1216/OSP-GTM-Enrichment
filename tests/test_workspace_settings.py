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
    restore_osp_from_legacy_config,
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
# Helpers for file-based tests
# ---------------------------------------------------------------------------

def _write_fake_cfg(path, name: str) -> None:
    """Write a fake ICP config with a distinctive company name to a file."""
    cfg = default_icp_config()
    cfg.company.name = name
    cfg.company.one_liner = f"One-liner for {name}"
    cfg.company.value_props = [f"Value prop from {name}"]
    path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")


def _patch_config_path(monkeypatch, path):
    """Patch both CONFIG_PATH and _CONFIG_PATH_RELATIVE in src.icp_config."""
    import src.icp_config as _m
    monkeypatch.setattr(_m, "CONFIG_PATH", path)
    monkeypatch.setattr(_m, "_CONFIG_PATH_RELATIVE", path)


# ---------------------------------------------------------------------------
# 9. Backfill from file preserves OSP settings
# ---------------------------------------------------------------------------

class TestBackfillOspIcpConfig:
    def test_backfill_reads_from_file_when_db_column_null(self, tmp_path, monkeypatch):
        osp_id = _seed_osp()
        config_file = tmp_path / "icp_config.json"
        _write_fake_cfg(config_file, "Backfill OSP Name")
        _patch_config_path(monkeypatch, config_file)

        result = backfill_osp_icp_config()
        assert result is True

        loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert loaded.company.name == "Backfill OSP Name"

    def test_backfill_skips_when_real_data_already_set(self, monkeypatch):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("Real OSP Data"), workspace_id=osp_id)
        result = backfill_osp_icp_config()
        assert result is False

        loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert loaded.company.name == "Real OSP Data"

    def test_backfill_preserves_code_defaults_when_explicitly_stored(self, tmp_path, monkeypatch):
        """Non-NULL icp_config is never overwritten by backfill, even if it equals code defaults.

        The old behaviour (overwrite code-defaults with file data) caused settings
        to reset on every reboot when the user hadn't changed anything.  The fix:
        any non-NULL value is treated as intentionally saved and preserved.
        """
        osp_id = _seed_osp()
        # Explicitly store code defaults in DB (simulates a fresh UI save without edits).
        from src.models import Workspace
        with session_scope() as session:
            ws = session.get(Workspace, osp_id)
            ws.icp_config = default_icp_config().model_dump()

        config_file = tmp_path / "icp_config.json"
        _write_fake_cfg(config_file, "Should Not Overwrite")
        _patch_config_path(monkeypatch, config_file)

        result = backfill_osp_icp_config()
        # Backfill must be skipped — icp_config is not NULL.
        assert result is False

        loaded = load_workspace_icp_config(workspace_id=osp_id)
        # The stored value (code defaults) must be preserved, not replaced by the file.
        assert loaded.company.name == default_icp_config().company.name

    def test_backfill_force_overwrites_real_data(self, tmp_path, monkeypatch):
        """force=True always overwrites even when non-default data is present."""
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("Old Real Data"), workspace_id=osp_id)

        config_file = tmp_path / "icp_config.json"
        _write_fake_cfg(config_file, "Forced New Data")
        _patch_config_path(monkeypatch, config_file)

        result = backfill_osp_icp_config(force=True)
        assert result is True

        loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert loaded.company.name == "Forced New Data"

    def test_backfill_skips_when_file_missing(self, tmp_path, monkeypatch):
        osp_id = _seed_osp()
        missing_file = tmp_path / "does_not_exist.json"
        _patch_config_path(monkeypatch, missing_file)

        result = backfill_osp_icp_config()
        assert result is False

    def test_backfill_does_not_affect_other_workspaces(self, tmp_path, monkeypatch):
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Other WS BF", slug="other-ws-bf", instantly_campaign_id="c-bf")
        save_workspace_icp_config(_make_client_cfg("Other Corp"), workspace_id=other_ws["id"])

        config_file = tmp_path / "icp_config.json"
        _write_fake_cfg(config_file, "OSP From File BF")
        _patch_config_path(monkeypatch, config_file)

        backfill_osp_icp_config()
        other_loaded = load_workspace_icp_config(workspace_id=other_ws["id"])
        assert other_loaded.company.name == "Other Corp"


# ---------------------------------------------------------------------------
# Restore from legacy config
# ---------------------------------------------------------------------------

class TestRestoreOspFromLegacyConfig:
    def test_restore_reads_file_content(self, tmp_path, monkeypatch):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("Current DB Data"), workspace_id=osp_id)

        config_file = tmp_path / "icp_config.json"
        _write_fake_cfg(config_file, "Legacy File Data")
        _patch_config_path(monkeypatch, config_file)

        info = restore_osp_from_legacy_config()
        assert info["file_exists"] is True
        assert info["file_company_name"] == "Legacy File Data"
        assert info["current_db_company_name"] == "Current DB Data"

    def test_restore_reports_file_missing(self, tmp_path, monkeypatch):
        _seed_osp()
        missing = tmp_path / "no_file.json"
        _patch_config_path(monkeypatch, missing)
        info = restore_osp_from_legacy_config()
        assert info["file_exists"] is False
        assert info["config"] is None

    def test_restore_force_applies_file_data(self, tmp_path, monkeypatch):
        osp_id = _seed_osp()
        save_workspace_icp_config(_make_osp_cfg("Old DB"), workspace_id=osp_id)

        config_file = tmp_path / "icp_config.json"
        _write_fake_cfg(config_file, "Restored From File")
        _patch_config_path(monkeypatch, config_file)

        backfill_osp_icp_config(force=True)
        loaded = load_workspace_icp_config(workspace_id=osp_id)
        assert loaded.company.name == "Restored From File"

    def test_restore_only_affects_osp(self, tmp_path, monkeypatch):
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Other RS", slug="other-rs", instantly_campaign_id="c-rs")
        save_workspace_icp_config(_make_client_cfg("Other Client RS"), workspace_id=other_ws["id"])

        config_file = tmp_path / "icp_config.json"
        _write_fake_cfg(config_file, "OSP Restored")
        _patch_config_path(monkeypatch, config_file)

        backfill_osp_icp_config(force=True)

        other_loaded = load_workspace_icp_config(workspace_id=other_ws["id"])
        assert other_loaded.company.name == "Other Client RS"


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
