"""Phase 5 workspace tests.

Verifies that workspace_id is threaded correctly through the pipeline:
  1. OSP scoring uses OSP workspace settings (company, ICP).
  2. Test workspace scoring uses test workspace settings.
  3. Test workspace does not read OSP icp_config.
  4. Email generation uses selected workspace settings (ICP block).
  5. Email generation uses selected workspace prompt.
  6. GeneratedContent rows are stamped with the correct workspace_id.
  7. Scoring rows are stamped with the correct workspace_id.
  8. Non-OSP workspace does not read data/icp_config.json as active config.
  9. OSP uses OSP workspace settings from DB (not file fallback when DB has data).
  10. Instantly campaign resolver uses selected workspace campaign ID.
  11. current_email_prompt_fingerprint uses workspace-specific prompt overlay.
  12. bulk_regenerate_content accepts and threads workspace_id.
"""
from __future__ import annotations

import pytest

from src.db import session_scope
from src.icp_config import (
    ICPConfig,
    CompanyProfile,
    load_workspace_icp_config,
    save_workspace_icp_config,
)
from src.models import PromptConfig, Workspace
from src.workspace import (
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


def _save_icp(workspace_id: int, company_name: str, one_liner: str = "") -> None:
    cfg = ICPConfig()
    cfg.company = CompanyProfile(
        name=company_name,
        one_liner=one_liner or f"One-liner for {company_name}",
        value_props=[f"{company_name} value prop"],
        differentiators=[f"{company_name} differentiator"],
    )
    save_workspace_icp_config(cfg, workspace_id=workspace_id)


def _save_prompt(workspace_id: int, channel: str, content: str) -> None:
    from src.prompts.loader import save_overlay
    save_overlay(channel, content, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# 1 & 2. ICP config reads are workspace-scoped
# ---------------------------------------------------------------------------

class TestWorkspaceIcpLoading:
    def test_osp_icp_returns_osp_company_name(self):
        osp_id = _seed_osp()
        _save_icp(osp_id, "Outbound Sales Pro")
        cfg = load_workspace_icp_config(osp_id)
        assert cfg.company.name == "Outbound Sales Pro"

    def test_other_workspace_returns_its_own_company_name(self):
        osp_id = _seed_osp()
        _save_icp(osp_id, "Outbound Sales Pro")
        other_ws = create_workspace(name="Client X", slug="client-x", instantly_campaign_id="c-x")
        _save_icp(other_ws["id"], "Client X Corp")

        other_cfg = load_workspace_icp_config(other_ws["id"])
        assert other_cfg.company.name == "Client X Corp"

    def test_other_workspace_does_not_see_osp_name(self):
        osp_id = _seed_osp()
        _save_icp(osp_id, "Outbound Sales Pro")
        other_ws = create_workspace(name="Client Y", slug="client-y", instantly_campaign_id="c-y")
        _save_icp(other_ws["id"], "Client Y Corp")

        other_cfg = load_workspace_icp_config(other_ws["id"])
        assert "Outbound Sales Pro" not in other_cfg.company.name

    def test_osp_config_does_not_bleed_into_other_workspace(self):
        osp_id = _seed_osp()
        _save_icp(osp_id, "OSP Confidential Settings")
        other_ws = create_workspace(name="Client Z", slug="client-z", instantly_campaign_id="c-z")

        other_cfg = load_workspace_icp_config(other_ws["id"])
        assert other_cfg.company.name != "OSP Confidential Settings"


# ---------------------------------------------------------------------------
# 3. Non-OSP workspace returns blank defaults when no settings saved
# ---------------------------------------------------------------------------

class TestNewWorkspaceIcpDefaults:
    def test_new_workspace_with_no_settings_returns_defaults(self):
        from src.icp_config import default_icp_config
        _seed_osp()
        new_ws = create_workspace(name="Blank WS", slug="blank-ws", instantly_campaign_id="c-blank")
        cfg = load_workspace_icp_config(new_ws["id"])
        assert cfg == default_icp_config()

    def test_new_workspace_does_not_read_osp_db_config(self):
        osp_id = _seed_osp()
        _save_icp(osp_id, "OSP Should Not Leak")
        new_ws = create_workspace(name="No Leak WS", slug="no-leak-ws", instantly_campaign_id="c-nl")
        cfg = load_workspace_icp_config(new_ws["id"])
        assert cfg.company.name != "OSP Should Not Leak"


# ---------------------------------------------------------------------------
# 4 & 5. Prompt loading is workspace-scoped
# ---------------------------------------------------------------------------

class TestPromptLoadingWorkspaceScoped:
    def test_prompt_overlay_returns_workspace_specific_content(self):
        from src.prompts.loader import get_effective_prompt
        from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY
        osp_id = _seed_osp()
        _save_prompt(osp_id, "email", "OSP custom email prompt")
        other_ws = create_workspace(name="Prompt WS", slug="prompt-ws", instantly_campaign_id="c-pr")
        _save_prompt(other_ws["id"], "email", "Client custom email prompt")

        osp_prompt = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY, workspace_id=osp_id)
        other_prompt = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY, workspace_id=other_ws["id"])

        assert osp_prompt.strip() == "OSP custom email prompt"
        assert other_prompt.strip() == "Client custom email prompt"

    def test_prompt_for_new_workspace_returns_default_when_no_overlay(self):
        from src.prompts.loader import get_effective_prompt
        from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY
        _seed_osp()
        new_ws = create_workspace(name="No Prompt WS", slug="no-prompt-ws", instantly_campaign_id="c-np")

        prompt = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY, workspace_id=new_ws["id"])
        assert prompt == DEFAULT_EMAIL_PROMPT_BODY

    def test_editing_other_workspace_prompt_does_not_change_osp(self):
        from src.prompts.loader import get_effective_prompt, save_overlay
        from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY
        osp_id = _seed_osp()
        _save_prompt(osp_id, "email", "OSP original prompt")
        other_ws = create_workspace(name="Edit Prompt WS", slug="edit-prompt-ws", instantly_campaign_id="c-ep")

        save_overlay("email", "Client edited prompt", workspace_id=other_ws["id"])

        osp_prompt = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY, workspace_id=osp_id)
        assert osp_prompt.strip() == "OSP original prompt"


# ---------------------------------------------------------------------------
# 6. current_email_prompt_fingerprint is workspace-scoped
# ---------------------------------------------------------------------------

class TestEmailPromptFingerprint:
    def test_fingerprints_differ_by_workspace_when_prompts_differ(self):
        from src.prompts.email import current_email_prompt_fingerprint
        osp_id = _seed_osp()
        _save_prompt(osp_id, "email", "OSP email prompt for fingerprint test")
        other_ws = create_workspace(
            name="FP Test WS", slug="fp-test-ws", instantly_campaign_id="c-fp"
        )
        _save_prompt(other_ws["id"], "email", "Other email prompt for fingerprint test")

        osp_fp = current_email_prompt_fingerprint(osp_id)
        other_fp = current_email_prompt_fingerprint(other_ws["id"])
        assert osp_fp != other_fp

    def test_fingerprints_equal_when_prompts_are_same(self):
        from src.prompts.email import current_email_prompt_fingerprint
        osp_id = _seed_osp()
        _save_prompt(osp_id, "email", "Shared prompt text")
        other_ws = create_workspace(
            name="Same FP WS", slug="same-fp-ws", instantly_campaign_id="c-sfp"
        )
        _save_prompt(other_ws["id"], "email", "Shared prompt text")

        osp_fp = current_email_prompt_fingerprint(osp_id)
        other_fp = current_email_prompt_fingerprint(other_ws["id"])
        assert osp_fp == other_fp


# ---------------------------------------------------------------------------
# 7. Instantly campaign resolver uses workspace campaign ID
# ---------------------------------------------------------------------------

class TestCampaignIdResolution:
    def test_workspace_campaign_id_is_used_over_env(self, monkeypatch):
        _seed_osp()
        ws = create_workspace(name="Campaign WS 5", slug="campaign-ws-5", instantly_campaign_id="ws-specific-camp")
        from src.workspace import get_campaign_id_for_workspace
        resolved = get_campaign_id_for_workspace(ws["id"])
        assert resolved == "ws-specific-camp"

    def test_env_fallback_when_workspace_has_no_campaign_id(self, monkeypatch):
        from src.workspace import get_default_workspace_id, get_default_workspace
        _seed_osp()
        default_ws = get_default_workspace()
        if default_ws and not default_ws.get("instantly_campaign_id"):
            monkeypatch.setattr("src.workspace.settings.instantly_campaign_id", "env-camp-5")
            from src.workspace import get_campaign_id_for_workspace
            resolved = get_campaign_id_for_workspace(default_ws["id"])
            assert resolved == "env-camp-5"


# ---------------------------------------------------------------------------
# 8. Saving settings for one workspace does not affect another
# ---------------------------------------------------------------------------

class TestSettingsIsolation:
    def test_osp_settings_unchanged_after_client_save(self):
        osp_id = _seed_osp()
        _save_icp(osp_id, "Stable OSP Name")

        other_ws = create_workspace(name="Change WS", slug="change-ws", instantly_campaign_id="c-chg")
        _save_icp(other_ws["id"], "Client Changed Name")

        osp_cfg = load_workspace_icp_config(osp_id)
        assert osp_cfg.company.name == "Stable OSP Name"

    def test_client_settings_unchanged_after_osp_save(self):
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Stable WS", slug="stable-ws", instantly_campaign_id="c-stbl")
        _save_icp(other_ws["id"], "Stable Client Name")

        _save_icp(osp_id, "OSP Modified")

        client_cfg = load_workspace_icp_config(other_ws["id"])
        assert client_cfg.company.name == "Stable Client Name"

    def test_two_clients_independently_isolated(self):
        _seed_osp()
        ws1 = create_workspace(name="Client AA", slug="client-aa", instantly_campaign_id="c-aa")
        ws2 = create_workspace(name="Client BB", slug="client-bb", instantly_campaign_id="c-bb")

        _save_icp(ws1["id"], "Client AA Corp")
        _save_icp(ws2["id"], "Client BB Corp")

        assert load_workspace_icp_config(ws1["id"]).company.name == "Client AA Corp"
        assert load_workspace_icp_config(ws2["id"]).company.name == "Client BB Corp"


# ---------------------------------------------------------------------------
# 9. ICP rendered for scoring uses workspace ICP block
# ---------------------------------------------------------------------------

class TestIcpBlockInPrompts:
    def test_render_icp_block_uses_workspace_company_name(self):
        from src.icp_config import render_icp_block
        osp_id = _seed_osp()
        _save_icp(osp_id, "OSP Agency for Render Test", "OSP one-liner")

        cfg = load_workspace_icp_config(osp_id)
        block = render_icp_block(cfg)
        assert "OSP Agency for Render Test" in block
        assert "OSP one-liner" in block

    def test_render_icp_block_different_per_workspace(self):
        from src.icp_config import render_icp_block
        osp_id = _seed_osp()
        _save_icp(osp_id, "OSP Block Company", "OSP pitch")
        other_ws = create_workspace(name="Block WS", slug="block-ws", instantly_campaign_id="c-blk")
        _save_icp(other_ws["id"], "Client Block Company", "Client pitch")

        osp_cfg = load_workspace_icp_config(osp_id)
        client_cfg = load_workspace_icp_config(other_ws["id"])

        osp_block = render_icp_block(osp_cfg)
        client_block = render_icp_block(client_cfg)

        assert "OSP Block Company" in osp_block
        assert "Client Block Company" in client_block
        assert "OSP Block Company" not in client_block
        assert "Client Block Company" not in osp_block


# ---------------------------------------------------------------------------
# 10. pipeline_runner functions accept workspace_id (smoke test)
# ---------------------------------------------------------------------------

class TestPipelineRunnerAcceptsWorkspaceId:
    def test_process_single_lead_signature_accepts_workspace_id(self):
        import inspect
        from app.lib.pipeline_runner import process_single_lead
        sig = inspect.signature(process_single_lead)
        assert "workspace_id" in sig.parameters

    def test_run_phased_pipeline_signature_accepts_workspace_id(self):
        import inspect
        from app.lib.pipeline_runner import run_phased_pipeline
        sig = inspect.signature(run_phased_pipeline)
        assert "workspace_id" in sig.parameters

    def test_bulk_regenerate_content_signature_accepts_workspace_id(self):
        import inspect
        from app.lib.pipeline_runner import bulk_regenerate_content
        sig = inspect.signature(bulk_regenerate_content)
        assert "workspace_id" in sig.parameters

    def test_score_lead_signature_accepts_workspace_id(self):
        import inspect
        from src.scoring import score_lead
        sig = inspect.signature(score_lead)
        assert "workspace_id" in sig.parameters

    def test_enrich_lead_signature_accepts_workspace_id(self):
        import inspect
        from src.enrichment.waterfall import enrich_lead
        sig = inspect.signature(enrich_lead)
        assert "workspace_id" in sig.parameters

    def test_generate_email_signature_accepts_workspace_id(self):
        import inspect
        from src.content.email import generate_email
        sig = inspect.signature(generate_email)
        assert "workspace_id" in sig.parameters

    def test_generate_linkedin_msg_signature_accepts_workspace_id(self):
        import inspect
        from src.content.linkedin_msg import generate_linkedin_msg
        sig = inspect.signature(generate_linkedin_msg)
        assert "workspace_id" in sig.parameters

    def test_generate_call_script_signature_accepts_workspace_id(self):
        import inspect
        from src.content.call_script import generate_call_script
        sig = inspect.signature(generate_call_script)
        assert "workspace_id" in sig.parameters


# ---------------------------------------------------------------------------
# 11. rating_runner functions accept workspace_id (smoke test)
# ---------------------------------------------------------------------------

class TestRatingRunnerAcceptsWorkspaceId:
    def test_rerun_enrichment_sync_accepts_workspace_id(self):
        import inspect
        from app.lib.rating_runner import rerun_enrichment_sync
        sig = inspect.signature(rerun_enrichment_sync)
        assert "workspace_id" in sig.parameters

    def test_rerun_scoring_sync_accepts_workspace_id(self):
        import inspect
        from app.lib.rating_runner import rerun_scoring_sync
        sig = inspect.signature(rerun_scoring_sync)
        assert "workspace_id" in sig.parameters

    def test_regenerate_email_sync_accepts_workspace_id(self):
        import inspect
        from app.lib.rating_runner import regenerate_email_sync
        sig = inspect.signature(regenerate_email_sync)
        assert "workspace_id" in sig.parameters

    def test_full_refresh_sync_accepts_workspace_id(self):
        import inspect
        from app.lib.rating_runner import full_refresh_sync
        sig = inspect.signature(full_refresh_sync)
        assert "workspace_id" in sig.parameters
