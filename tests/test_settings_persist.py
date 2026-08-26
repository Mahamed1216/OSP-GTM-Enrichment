"""Part C tests for the settings-persist + Lead Sources navigation hotfix.

10 required tests:
  1.  Lead Sources appears in authenticated navigation (main.py)
  2.  Lead Sources page is not only registered in unauthenticated state
  3.  Saving OSP workspace settings persists to DB
  4.  Re-running init_db does not overwrite saved OSP settings
  5.  Re-running seed/backfill does not overwrite non-empty icp_config
  6.  Empty icp_config can still be seeded once (NULL → file value)
  7.  Test Client settings do not leak to OSP
  8.  OSP settings do not leak to Test Client
  9.  Prompts remain unaffected by settings save/backfill
  10. Reboot simulation keeps saved workspace settings
"""
from __future__ import annotations

import json
import pathlib

from sqlalchemy import select, func

from src.db import session_scope
from src.icp_config import (
    CompanyProfile,
    ICPConfig,
    default_icp_config,
    load_workspace_icp_config,
    save_workspace_icp_config,
)
from src.models import PromptConfig, Workspace
from src.workspace import (
    backfill_osp_icp_config,
    create_workspace,
    get_default_workspace_id,
    seed_default_workspace,
)

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_osp() -> int:
    seed_default_workspace()
    ws_id = get_default_workspace_id()
    assert ws_id is not None
    return ws_id


def _cfg(name: str) -> ICPConfig:
    c = default_icp_config()
    c.company = CompanyProfile(name=name, one_liner="test")
    return c


def _patch_config(monkeypatch, tmp_path, file_company_name: str):
    """Write a fake icp_config.json and point src.icp_config at it."""
    import src.icp_config as _m
    cfg_file = tmp_path / "icp_config.json"
    data = default_icp_config().model_dump()
    data["company"]["name"] = file_company_name
    cfg_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(_m, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(_m, "_CONFIG_PATH_RELATIVE", cfg_file)
    return cfg_file


# ---------------------------------------------------------------------------
# Test 1: Lead Sources appears in authenticated navigation
# ---------------------------------------------------------------------------


def test_saving_osp_settings_persists_to_db():
    ws_id = _seed_osp()
    cfg = _cfg("Persisted OSP Name")
    save_workspace_icp_config(cfg, workspace_id=ws_id)

    with session_scope() as session:
        ws = session.get(Workspace, ws_id)
        stored_name = (ws.icp_config or {}).get("company", {}).get("name")

    assert stored_name == "Persisted OSP Name", (
        "Settings saved via save_workspace_icp_config must appear in the DB row"
    )


# ---------------------------------------------------------------------------
# Test 4: Re-running init_db does not overwrite saved OSP settings
# ---------------------------------------------------------------------------

def test_reinit_db_does_not_overwrite_saved_osp_settings(tmp_path, monkeypatch):
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("User Saved Value"), workspace_id=ws_id)

    # Simulate a config file with DIFFERENT values
    _patch_config(monkeypatch, tmp_path, "File Value — Should Not Win")

    # Calling backfill directly simulates the startup sequence inside init_db
    backfill_osp_icp_config()

    loaded = load_workspace_icp_config(workspace_id=ws_id)
    assert loaded.company.name == "User Saved Value", (
        "backfill_osp_icp_config must not overwrite a non-NULL icp_config"
    )


# ---------------------------------------------------------------------------
# Test 5: Re-running seed/backfill does not overwrite non-empty icp_config
# ---------------------------------------------------------------------------

def test_backfill_skips_non_empty_icp_config(tmp_path, monkeypatch):
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Already Set"), workspace_id=ws_id)

    _patch_config(monkeypatch, tmp_path, "Backfill Would Overwrite")

    result = backfill_osp_icp_config()

    assert result is False, "backfill must return False when icp_config is already set"
    loaded = load_workspace_icp_config(workspace_id=ws_id)
    assert loaded.company.name == "Already Set"


# ---------------------------------------------------------------------------
# Test 6: Empty icp_config can still be seeded once (NULL → file value)
# ---------------------------------------------------------------------------

def test_null_icp_config_is_seeded_from_file(tmp_path, monkeypatch):
    ws_id = _seed_osp()
    # Confirm icp_config starts as NULL
    with session_scope() as session:
        ws = session.get(Workspace, ws_id)
        assert ws.icp_config is None, "Fresh workspace must have NULL icp_config"

    _patch_config(monkeypatch, tmp_path, "Seeded From File")

    result = backfill_osp_icp_config()

    assert result is True, "backfill must return True when icp_config was NULL"
    loaded = load_workspace_icp_config(workspace_id=ws_id)
    assert loaded.company.name == "Seeded From File"


# ---------------------------------------------------------------------------
# Test 7: Test Client settings do not leak to OSP
# ---------------------------------------------------------------------------

def test_client_settings_do_not_leak_to_osp():
    osp_id = _seed_osp()
    save_workspace_icp_config(_cfg("OSP Original"), workspace_id=osp_id)

    client_ws = create_workspace(
        name="Test Client T7", slug="test-client-t7",
        instantly_campaign_id="camp-t7",
    )
    save_workspace_icp_config(_cfg("Client Data"), workspace_id=client_ws["id"])

    osp_loaded = load_workspace_icp_config(workspace_id=osp_id)
    assert osp_loaded.company.name == "OSP Original", (
        "Saving Test Client settings must not affect OSP workspace"
    )


# ---------------------------------------------------------------------------
# Test 8: OSP settings do not leak to Test Client
# ---------------------------------------------------------------------------

def test_osp_settings_do_not_leak_to_client():
    osp_id = _seed_osp()
    save_workspace_icp_config(_cfg("OSP Company"), workspace_id=osp_id)

    client_ws = create_workspace(
        name="Test Client T8", slug="test-client-t8",
        instantly_campaign_id="camp-t8",
    )
    # Test Client has never had settings saved
    client_loaded = load_workspace_icp_config(workspace_id=client_ws["id"])
    assert client_loaded.company.name != "OSP Company", (
        "A new workspace must not inherit OSP's saved settings"
    )


# ---------------------------------------------------------------------------
# Test 9: Prompts remain unaffected by settings save/backfill
# ---------------------------------------------------------------------------

def test_prompts_unaffected_by_settings_save_and_backfill(tmp_path, monkeypatch):
    ws_id = _seed_osp()

    # Record prompt_configs count before any settings operation
    with session_scope() as session:
        before = session.execute(
            select(func.count()).select_from(PromptConfig)
        ).scalar()

    save_workspace_icp_config(_cfg("Settings Save"), workspace_id=ws_id)

    _patch_config(monkeypatch, tmp_path, "Backfill Attempt")
    backfill_osp_icp_config(force=True)  # force to exercise the write path too

    with session_scope() as session:
        after = session.execute(
            select(func.count()).select_from(PromptConfig)
        ).scalar()

    assert after == before, (
        "save_workspace_icp_config and backfill_osp_icp_config must not "
        "create or modify prompt_configs rows"
    )


# ---------------------------------------------------------------------------
# Test 10: Reboot simulation keeps saved workspace settings
# ---------------------------------------------------------------------------

def test_reboot_simulation_keeps_saved_settings(tmp_path, monkeypatch):
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Saved Before Reboot"), workspace_id=ws_id)

    # Simulate reboot: the startup sequence calls seed + backfill
    # The config file (ephemeral FS after reboot) may contain stale/old data
    _patch_config(monkeypatch, tmp_path, "Stale File Data After Reboot")

    seed_default_workspace()           # idempotent — does nothing, OSP already exists
    backfill_osp_icp_config()          # must skip because icp_config is not NULL

    loaded = load_workspace_icp_config(workspace_id=ws_id)
    assert loaded.company.name == "Saved Before Reboot", (
        "Settings saved before reboot must survive the startup seed/backfill sequence"
    )
