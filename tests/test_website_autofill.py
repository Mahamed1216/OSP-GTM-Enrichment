"""Phase 9 tests: website autofill settings.

11 required tests:
  1.  Website analysis creates suggested settings but does not save automatically
  2.  Apply suggested settings saves only after explicit call to save_workspace_icp_config
  3.  Only checked fields are saved
  4.  Existing unchecked fields are preserved
  5.  Suggestions save to selected workspace only
  6.  OSP settings do not leak to Test Client
  7.  Test Client settings do not leak to OSP
  8.  Prompt configs are not modified by analysis or apply
  9.  init_db/reboot does not trigger website autofill
  10. Failed website fetch does not overwrite settings
  11. API keys or secrets are not exposed in apply output
"""
from __future__ import annotations

import pathlib

import httpx
import pytest
from sqlalchemy import func, select

from src.db import session_scope
from src.icp_config import (
    BuyerPersona,
    CompanyProfile,
    ICPConfig,
    ICPDefinition,
    IntentSignals,
    default_icp_config,
    load_workspace_icp_config,
    save_workspace_icp_config,
)
from src.models import PromptConfig
from src.website_analyzer import (
    AnalysisResult,
    WebsiteSuggestions,
    analyze_website_async,
    apply_suggestions_to_config,
)
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
    c.company = CompanyProfile(name=name, one_liner="original")
    return c


def _suggestions(**overrides) -> WebsiteSuggestions:
    base = dict(
        company_name="Acme Inc",
        one_liner="Helps B2B SaaS teams automate outbound prospecting",
        value_props=["Lead enrichment", "Personalized messaging"],
        differentiators=["AI-powered", "Plug-and-play"],
        target_industries=["B2B SaaS", "Fintech"],
        target_company_sizes=["50-500 employees"],
        target_company_stages=["Series A", "Series B"],
        target_tech_stack_signals=["Salesforce", "HubSpot"],
        target_geographies=["US", "Canada"],
        target_titles=["VP of Sales", "CRO"],
        seniority_levels=["VP", "Director"],
        departments=["Sales", "RevOps"],
        top_pain_points=["manual prospecting", "low reply rates"],
        common_objections=["already have a tool"],
        positive_signals=["hiring SDRs", "Series B funding"],
        disqualifiers=["B2C only"],
        news_search_terms=["outbound sales", "SDR automation"],
    )
    base.update(overrides)
    return WebsiteSuggestions(**base)


# ---------------------------------------------------------------------------
# 1. Analysis does not auto-save
# ---------------------------------------------------------------------------

async def test_analysis_does_not_auto_save(monkeypatch):
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Original Name"), workspace_id=ws_id)

    async def _mock_fetch(url):
        return "Acme Inc", "Acme helps B2B SaaS teams automate outbound."

    async def _mock_llm(content, url, notes):
        from src.website_analyzer import _LLMOutput
        return _LLMOutput(company_name="Should Not Save", one_liner="Test")

    import src.website_analyzer as _wa
    monkeypatch.setattr(_wa, "_fetch_html", _mock_fetch)
    monkeypatch.setattr(_wa, "_call_llm", _mock_llm)

    result = await analyze_website_async("https://acme.com")

    assert result.error is None
    assert result.suggestions.company_name == "Should Not Save"

    loaded = load_workspace_icp_config(workspace_id=ws_id)
    assert loaded.company.name == "Original Name", (
        "analyze_website_async must not auto-save to the DB"
    )


# ---------------------------------------------------------------------------
# 2. Apply requires explicit save call
# ---------------------------------------------------------------------------

def test_apply_requires_explicit_save_call():
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Pre-Apply Name"), workspace_id=ws_id)

    suggestions = _suggestions(company_name="Post-Apply Name")
    cfg = load_workspace_icp_config(workspace_id=ws_id)

    # apply_suggestions_to_config returns a new config but does NOT touch the DB
    updated = apply_suggestions_to_config(cfg, suggestions, {"company_name"})

    # DB unchanged
    db_val = load_workspace_icp_config(workspace_id=ws_id)
    assert db_val.company.name == "Pre-Apply Name", (
        "apply_suggestions_to_config must not write to the DB — caller must save explicitly"
    )

    # Explicit save applies the change
    save_workspace_icp_config(updated, workspace_id=ws_id)
    assert load_workspace_icp_config(workspace_id=ws_id).company.name == "Post-Apply Name"


# ---------------------------------------------------------------------------
# 3. Only checked fields are saved
# ---------------------------------------------------------------------------

def test_only_checked_fields_are_saved():
    suggestions = _suggestions(
        company_name="New Name",
        one_liner="New one-liner",
        target_industries=["HealthTech"],
    )
    cfg = default_icp_config()
    cfg.company = CompanyProfile(name="Old Name", one_liner="Old one-liner")
    cfg.icp = ICPDefinition(target_industries=["B2B SaaS"])

    # Apply only company_name
    updated = apply_suggestions_to_config(cfg, suggestions, {"company_name"})

    assert updated.company.name == "New Name"
    assert updated.company.one_liner == "Old one-liner"
    assert updated.icp.target_industries == ["B2B SaaS"]


# ---------------------------------------------------------------------------
# 4. Unchecked fields are preserved
# ---------------------------------------------------------------------------

def test_unchecked_fields_are_preserved():
    existing = default_icp_config()
    existing.persona = BuyerPersona(
        target_titles=["Keep This Title"],
        top_pain_points=["keep this pain"],
    )

    suggestions = _suggestions(
        target_titles=["New Title"],
        top_pain_points=["new pain"],
    )

    # Check target_titles only
    updated = apply_suggestions_to_config(existing, suggestions, {"target_titles"})

    assert updated.persona.target_titles == ["New Title"]
    assert updated.persona.top_pain_points == ["keep this pain"], (
        "Unchecked fields must be preserved from the original config"
    )


# ---------------------------------------------------------------------------
# 5. Suggestions save to selected workspace only
# ---------------------------------------------------------------------------

def test_suggestions_save_to_selected_workspace_only():
    ws1_id = _seed_osp()
    save_workspace_icp_config(_cfg("Workspace 1"), workspace_id=ws1_id)

    ws2 = create_workspace(
        name="Test Client T5", slug="test-client-t5",
        instantly_campaign_id="camp-t5",
    )
    ws2_id = ws2["id"]
    save_workspace_icp_config(_cfg("Workspace 2"), workspace_id=ws2_id)

    suggestions = _suggestions(company_name="Applied To WS1")
    cfg1 = load_workspace_icp_config(workspace_id=ws1_id)
    updated = apply_suggestions_to_config(cfg1, suggestions, {"company_name"})
    save_workspace_icp_config(updated, workspace_id=ws1_id)

    assert load_workspace_icp_config(workspace_id=ws1_id).company.name == "Applied To WS1"
    assert load_workspace_icp_config(workspace_id=ws2_id).company.name == "Workspace 2", (
        "Applying suggestions to workspace 1 must not affect workspace 2"
    )


# ---------------------------------------------------------------------------
# 6. OSP settings do not leak to Test Client
# ---------------------------------------------------------------------------

def test_osp_settings_do_not_leak_to_test_client():
    osp_id = _seed_osp()
    save_workspace_icp_config(_cfg("OSP Settings"), workspace_id=osp_id)

    client_ws = create_workspace(
        name="Test Client T6", slug="test-client-t6",
        instantly_campaign_id="camp-t6",
    )
    client_id = client_ws["id"]
    save_workspace_icp_config(_cfg("Client Settings"), workspace_id=client_id)

    suggestions = _suggestions(company_name="Applied To OSP")
    osp_cfg = load_workspace_icp_config(workspace_id=osp_id)
    updated = apply_suggestions_to_config(osp_cfg, suggestions, {"company_name"})
    save_workspace_icp_config(updated, workspace_id=osp_id)

    client_loaded = load_workspace_icp_config(workspace_id=client_id)
    assert client_loaded.company.name == "Client Settings", (
        "Applying suggestions to OSP must not change Test Client settings"
    )


# ---------------------------------------------------------------------------
# 7. Test Client settings do not leak to OSP
# ---------------------------------------------------------------------------

def test_client_settings_do_not_leak_to_osp():
    osp_id = _seed_osp()
    save_workspace_icp_config(_cfg("OSP Company"), workspace_id=osp_id)

    client_ws = create_workspace(
        name="Test Client T7", slug="test-client-t7",
        instantly_campaign_id="camp-t7",
    )
    client_id = client_ws["id"]
    save_workspace_icp_config(_cfg("Client Company"), workspace_id=client_id)

    suggestions = _suggestions(company_name="Applied To Client")
    client_cfg = load_workspace_icp_config(workspace_id=client_id)
    updated = apply_suggestions_to_config(client_cfg, suggestions, {"company_name"})
    save_workspace_icp_config(updated, workspace_id=client_id)

    osp_loaded = load_workspace_icp_config(workspace_id=osp_id)
    assert osp_loaded.company.name == "OSP Company", (
        "Applying suggestions to Test Client must not change OSP settings"
    )


# ---------------------------------------------------------------------------
# 8. Prompt configs are not modified
# ---------------------------------------------------------------------------

def test_prompt_configs_not_modified():
    ws_id = _seed_osp()

    with session_scope() as session:
        before = session.execute(
            select(func.count()).select_from(PromptConfig)
        ).scalar()

    suggestions = _suggestions()
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    all_fields = set(WebsiteSuggestions.model_fields.keys())
    updated = apply_suggestions_to_config(cfg, suggestions, all_fields)
    save_workspace_icp_config(updated, workspace_id=ws_id)

    with session_scope() as session:
        after = session.execute(
            select(func.count()).select_from(PromptConfig)
        ).scalar()

    assert after == before, (
        "apply_suggestions_to_config and save must not create or modify PromptConfig rows"
    )


# ---------------------------------------------------------------------------
# 9. init_db/reboot does not trigger website autofill
# ---------------------------------------------------------------------------

def test_init_db_does_not_trigger_website_autofill():
    db_path = _PROJECT_ROOT / "src" / "db.py"
    workspace_path = _PROJECT_ROOT / "src" / "workspace.py"

    db_text = db_path.read_text(encoding="utf-8")
    workspace_text = workspace_path.read_text(encoding="utf-8")

    assert "website_analyzer" not in db_text, (
        "src/db.py must not import website_analyzer"
    )
    assert "analyze_website" not in workspace_text, (
        "src/workspace.py seed/backfill must not call analyze_website"
    )

    # Simulate startup — must not touch website analyzer
    seed_default_workspace()
    backfill_osp_icp_config()


# ---------------------------------------------------------------------------
# 10. Failed website fetch does not overwrite settings
# ---------------------------------------------------------------------------

async def test_failed_fetch_does_not_overwrite_settings(monkeypatch):
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Protected Name"), workspace_id=ws_id)

    async def _fail_fetch(url):
        raise httpx.ConnectError("Connection refused")

    async def _fail_tavily(domain, notes):
        return [], ""

    import src.website_analyzer as _wa
    monkeypatch.setattr(_wa, "_fetch_html", _fail_fetch)
    monkeypatch.setattr(_wa, "_search_tavily", _fail_tavily)

    result = await analyze_website_async("https://unreachable.example.com")

    assert result.error is not None, "Failed fetch must return an error"
    assert result.suggestions.company_name == "", (
        "Failed fetch must not produce suggestions"
    )

    loaded = load_workspace_icp_config(workspace_id=ws_id)
    assert loaded.company.name == "Protected Name", (
        "Failed website fetch must not overwrite saved settings"
    )


# ---------------------------------------------------------------------------
# 11. API keys are not exposed in apply output
# ---------------------------------------------------------------------------

def test_api_keys_not_exposed_in_apply_output(monkeypatch):
    from src.config import settings as _settings
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-fake-secret-99999")
    monkeypatch.setattr(_settings, "tavily_api_key", "tvly-fake-secret-99999")

    suggestions = _suggestions(company_name="Safe Company", one_liner="Legitimate")
    cfg = default_icp_config()
    all_fields = set(WebsiteSuggestions.model_fields.keys())
    updated = apply_suggestions_to_config(cfg, suggestions, all_fields)

    cfg_str = str(updated.model_dump())
    assert "sk-fake-secret-99999" not in cfg_str, (
        "apply_suggestions_to_config must not include the Anthropic API key in output"
    )
    assert "tvly-fake-secret-99999" not in cfg_str, (
        "apply_suggestions_to_config must not include the Tavily API key in output"
    )


# ===========================================================================
# Hotfix tests: robust JSON parsing and repair fallback
# ===========================================================================

# ---------------------------------------------------------------------------
# H1. Valid JSON parses correctly
# ---------------------------------------------------------------------------

def test_parse_valid_json():
    from src.website_analyzer import _parse_llm_json
    raw = '{"company_name": "Acme Corp", "one_liner": "Test one-liner", "confidence": "high"}'
    result = _parse_llm_json(raw)
    assert result.company_name == "Acme Corp"
    assert result.one_liner == "Test one-liner"
    assert result.confidence == "high"
    assert result.value_props == []


# ---------------------------------------------------------------------------
# H2. JSON inside a markdown code fence parses correctly
# ---------------------------------------------------------------------------

def test_parse_json_in_code_fence():
    from src.website_analyzer import _parse_llm_json
    raw = '```json\n{"company_name": "Fenced Corp", "one_liner": "inside fence"}\n```'
    result = _parse_llm_json(raw)
    assert result.company_name == "Fenced Corp"
    assert result.one_liner == "inside fence"


def test_parse_json_in_plain_fence():
    from src.website_analyzer import _parse_llm_json
    raw = '```\n{"company_name": "Plain Fence"}\n```'
    result = _parse_llm_json(raw)
    assert result.company_name == "Plain Fence"


# ---------------------------------------------------------------------------
# H3. Broken JSON triggers repair fallback
# ---------------------------------------------------------------------------

async def test_broken_json_triggers_repair_fallback(monkeypatch):
    import src.website_analyzer as _wa
    calls: list[str] = []

    async def _mock_raw(system: str, user: str, max_tokens: int = 4000) -> str:
        calls.append("repair" if "malformed" in user else "first")
        if len(calls) == 1:
            # Simulate truncated response (missing closing brace/quote)
            return '{"company_name": "Acme", "one_liner": "truncated...'
        # Repair response: valid JSON
        return '{"company_name": "Acme Repaired", "one_liner": "fixed output"}'

    monkeypatch.setattr(_wa, "_raw_llm_call", _mock_raw)

    result = await _wa._call_llm("some website content", "https://example.com", "")

    assert len(calls) == 2, "Must make exactly 2 calls: first attempt + repair"
    assert calls[0] == "first"
    assert calls[1] == "repair"
    assert result.company_name == "Acme Repaired"
    assert result.one_liner == "fixed output"


# ---------------------------------------------------------------------------
# H4. If repair fails, no settings are saved
# ---------------------------------------------------------------------------

async def test_repair_failure_does_not_save_settings(monkeypatch):
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Protected"), workspace_id=ws_id)

    async def _always_bad(system: str, user: str, max_tokens: int = 4000) -> str:
        return "{ this is definitely not valid json }"

    async def _mock_fetch(url: str) -> tuple[str, str]:
        return "Title", "Some content about a company"

    import src.website_analyzer as _wa
    monkeypatch.setattr(_wa, "_raw_llm_call", _always_bad)
    monkeypatch.setattr(_wa, "_fetch_html", _mock_fetch)

    result = await analyze_website_async("https://example.com")

    assert result.error is not None, "Should surface an error when repair fails"
    assert result.suggestions.company_name == "", "No suggestions should be set on failure"

    loaded = load_workspace_icp_config(workspace_id=ws_id)
    assert loaded.company.name == "Protected", (
        "Settings must remain unchanged when LLM output cannot be parsed"
    )


# ---------------------------------------------------------------------------
# H5. Missing fields do not crash — defaults apply
# ---------------------------------------------------------------------------

def test_missing_fields_do_not_crash():
    from src.website_analyzer import _parse_llm_json
    # Minimal JSON with only one field — all others should default
    raw = '{"company_name": "Minimal Co"}'
    result = _parse_llm_json(raw)
    assert result.company_name == "Minimal Co"
    assert result.one_liner == ""
    assert result.value_props == []
    assert result.target_industries == []
    assert result.top_pain_points == []
    assert result.confidence == "medium"
    assert result.reasoning == {}


# ---------------------------------------------------------------------------
# H6. After repair path, suggestions are still workspace-scoped
# ---------------------------------------------------------------------------

def test_repaired_suggestions_are_workspace_scoped():
    ws1_id = _seed_osp()
    ws2 = create_workspace(
        name="Test Client H6", slug="test-client-h6",
        instantly_campaign_id="camp-h6",
    )
    ws2_id = ws2["id"]
    save_workspace_icp_config(_cfg("WS1 Original"), workspace_id=ws1_id)
    save_workspace_icp_config(_cfg("WS2 Original"), workspace_id=ws2_id)

    # Simulate result from repair path (AnalysisResult with valid suggestions)
    sugg = _suggestions(company_name="WS1 Updated via Repair")
    cfg1 = load_workspace_icp_config(workspace_id=ws1_id)
    updated = apply_suggestions_to_config(cfg1, sugg, {"company_name"})
    save_workspace_icp_config(updated, workspace_id=ws1_id)

    assert load_workspace_icp_config(workspace_id=ws1_id).company.name == "WS1 Updated via Repair"
    assert load_workspace_icp_config(workspace_id=ws2_id).company.name == "WS2 Original", (
        "Saving repaired suggestions to workspace 1 must not affect workspace 2"
    )


# ---------------------------------------------------------------------------
# H7. Apply still requires confirmation even after repair path produces result
# ---------------------------------------------------------------------------

def test_apply_still_requires_confirmation_after_repair():
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Before Repair"), workspace_id=ws_id)

    # Simulate a result that came through the repair path
    sugg = _suggestions(company_name="After Repair Apply")
    repaired_result = AnalysisResult(
        suggestions=sugg,
        sources_used=["https://example.com"],
        confidence="high",
    )

    # Creating AnalysisResult does NOT touch DB
    assert load_workspace_icp_config(workspace_id=ws_id).company.name == "Before Repair"

    # apply_suggestions_to_config also does NOT touch DB
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    updated = apply_suggestions_to_config(cfg, repaired_result.suggestions, {"company_name"})
    assert load_workspace_icp_config(workspace_id=ws_id).company.name == "Before Repair"

    # Only explicit save changes the DB
    save_workspace_icp_config(updated, workspace_id=ws_id)
    assert load_workspace_icp_config(workspace_id=ws_id).company.name == "After Repair Apply"


# ---------------------------------------------------------------------------
# H8. Prompt configs are not touched by repair path or apply
# ---------------------------------------------------------------------------

def test_prompt_configs_not_touched_after_repair_path():
    ws_id = _seed_osp()

    with session_scope() as s:
        before = s.execute(select(func.count()).select_from(PromptConfig)).scalar()

    sugg = _suggestions()
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    all_fields = set(WebsiteSuggestions.model_fields.keys())
    updated = apply_suggestions_to_config(cfg, sugg, all_fields)
    save_workspace_icp_config(updated, workspace_id=ws_id)

    with session_scope() as s:
        after = s.execute(select(func.count()).select_from(PromptConfig)).scalar()

    assert after == before, (
        "PromptConfig rows must be unchanged after apply_suggestions + save"
    )


# ===========================================================================
# Hotfix tests: apply actually persists to workspace.icp_config
# ===========================================================================

# Field → form session-state key map (mirrors _AUTOFILL_TO_FORM_KEY in 6_settings.py)
_FORM_KEYS = {
    "company_name":               "s_company_name",
    "one_liner":                  "s_company_one_liner",
    "value_props":                "s_company_value_props",
    "differentiators":            "s_company_differentiators",
    "target_industries":          "s_icp_industries",
    "target_company_sizes":       "s_icp_sizes",
    "target_company_stages":      "s_icp_stages",
    "target_tech_stack_signals":  "s_icp_tech",
    "target_geographies":         "s_icp_geos",
    "target_titles":              "s_persona_titles",
    "seniority_levels":           "s_persona_seniority",
    "departments":                "s_persona_departments",
    "top_pain_points":            "s_persona_pains",
    "common_objections":          "s_persona_objections",
    "positive_signals":           "s_signals_positive",
    "disqualifiers":              "s_signals_disqualifiers",
    "news_search_terms":          "s_news_terms",
}


# ---------------------------------------------------------------------------
# S1. Apply updates workspace.icp_config in the DB
# ---------------------------------------------------------------------------

def test_apply_updates_workspace_icp_config_in_db():
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Before Apply"), workspace_id=ws_id)

    sugg = _suggestions(company_name="After Apply", one_liner="New one-liner")
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    updated = apply_suggestions_to_config(cfg, sugg, {"company_name", "one_liner"})
    save_workspace_icp_config(updated, workspace_id=ws_id)

    # Read back directly from the DB row to confirm icp_config JSON was updated
    with session_scope() as session:
        from src.models import Workspace
        ws_row = session.get(Workspace, ws_id)
        stored = ws_row.icp_config or {}

    assert stored.get("company", {}).get("name") == "After Apply", (
        "workspace.icp_config['company']['name'] must be updated after apply+save"
    )
    assert stored.get("company", {}).get("one_liner") == "New one-liner"


# ---------------------------------------------------------------------------
# S2. Saved keys match the Settings page ICPConfig structure
# ---------------------------------------------------------------------------

def test_saved_keys_match_settings_page_structure():
    ws_id = _seed_osp()
    sugg = _suggestions(
        company_name="Struct Test",
        target_industries=["SaaS", "Fintech"],
        top_pain_points=["manual prospecting"],
        positive_signals=["hiring SDRs"],
        news_search_terms=["outbound sales"],
    )
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    all_fields = set(WebsiteSuggestions.model_fields.keys())
    updated = apply_suggestions_to_config(cfg, sugg, all_fields)
    save_workspace_icp_config(updated, workspace_id=ws_id)

    # Read back via load_workspace_icp_config — must reconstruct correctly
    reloaded = load_workspace_icp_config(workspace_id=ws_id)

    # Verify the structure matches what the Settings page reads
    assert reloaded.company.name == "Struct Test"
    assert "SaaS" in reloaded.icp.target_industries
    assert "manual prospecting" in reloaded.persona.top_pain_points
    assert "hiring SDRs" in reloaded.signals.positive_signals
    assert "outbound sales" in reloaded.news_search_terms


# ---------------------------------------------------------------------------
# S3. After apply, loading settings returns the new values
# ---------------------------------------------------------------------------

def test_after_apply_load_returns_new_values():
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Old Value"), workspace_id=ws_id)

    sugg = _suggestions(company_name="New Value", one_liner="Updated liner")
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    updated = apply_suggestions_to_config(cfg, sugg, {"company_name", "one_liner"})
    save_workspace_icp_config(updated, workspace_id=ws_id)

    # Simulate what the Settings page does on next rerun: load from DB
    reloaded = load_workspace_icp_config(workspace_id=ws_id)
    assert reloaded.company.name == "New Value"
    assert reloaded.company.one_liner == "Updated liner"


# ---------------------------------------------------------------------------
# S4. Only checked fields are written to icp_config
# ---------------------------------------------------------------------------

def test_only_checked_fields_written_to_icp_config():
    ws_id = _seed_osp()
    initial = default_icp_config()
    initial.company = CompanyProfile(name="Keep Me", one_liner="Keep this too")
    initial.icp = ICPDefinition(target_industries=["Keep Industry"])
    save_workspace_icp_config(initial, workspace_id=ws_id)

    sugg = _suggestions(
        company_name="Replace Me",
        one_liner="Replace this",
        target_industries=["Replace Industry"],
    )
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    updated = apply_suggestions_to_config(cfg, sugg, {"company_name"})
    save_workspace_icp_config(updated, workspace_id=ws_id)

    reloaded = load_workspace_icp_config(workspace_id=ws_id)
    assert reloaded.company.name == "Replace Me"
    assert reloaded.company.one_liner == "Keep this too", "Unchecked field must not change"
    assert reloaded.icp.target_industries == ["Keep Industry"], "Unchecked field must not change"


# ---------------------------------------------------------------------------
# S5. Unchecked fields are preserved in icp_config after apply
# ---------------------------------------------------------------------------

def test_unchecked_fields_preserved_in_icp_config():
    ws_id = _seed_osp()
    initial = default_icp_config()
    initial.persona = BuyerPersona(
        target_titles=["Preserved Title"],
        top_pain_points=["preserved pain"],
        seniority_levels=["VP"],
    )
    save_workspace_icp_config(initial, workspace_id=ws_id)

    sugg = _suggestions(
        target_titles=["New Title"],
        top_pain_points=["new pain"],
        seniority_levels=["Director"],
    )
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    # Only apply target_titles, not the rest of persona
    updated = apply_suggestions_to_config(cfg, sugg, {"target_titles"})
    save_workspace_icp_config(updated, workspace_id=ws_id)

    reloaded = load_workspace_icp_config(workspace_id=ws_id)
    assert reloaded.persona.target_titles == ["New Title"]
    assert reloaded.persona.top_pain_points == ["preserved pain"]
    assert reloaded.persona.seniority_levels == ["VP"]


# ---------------------------------------------------------------------------
# S6. No fields checked → no DB write, no success (pure logic test)
# ---------------------------------------------------------------------------

def test_no_fields_checked_does_not_save():
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Untouched"), workspace_id=ws_id)

    sugg = _suggestions(company_name="Would Replace")
    cfg = load_workspace_icp_config(workspace_id=ws_id)

    # Simulate "no fields checked" — apply with empty set
    updated = apply_suggestions_to_config(cfg, sugg, set())
    # Nothing checked → caller must NOT save; verify apply returns original values
    assert updated.company.name == "Untouched", (
        "apply_suggestions_to_config with empty checked_fields must return original config"
    )

    # Verify DB is still untouched (no save was called)
    reloaded = load_workspace_icp_config(workspace_id=ws_id)
    assert reloaded.company.name == "Untouched"


# ---------------------------------------------------------------------------
# S7. Apply in Test Client does not change OSP
# ---------------------------------------------------------------------------

def test_apply_in_test_client_does_not_change_osp():
    osp_id = _seed_osp()
    save_workspace_icp_config(_cfg("OSP Untouched"), workspace_id=osp_id)

    client_ws = create_workspace(
        name="Test Client S7", slug="test-client-s7",
        instantly_campaign_id="camp-s7",
    )
    client_id = client_ws["id"]
    save_workspace_icp_config(_cfg("Client Before"), workspace_id=client_id)

    sugg = _suggestions(company_name="Client After")
    client_cfg = load_workspace_icp_config(workspace_id=client_id)
    updated = apply_suggestions_to_config(client_cfg, sugg, {"company_name"})
    save_workspace_icp_config(updated, workspace_id=client_id)

    osp_reloaded = load_workspace_icp_config(workspace_id=osp_id)
    client_reloaded = load_workspace_icp_config(workspace_id=client_id)

    assert client_reloaded.company.name == "Client After"
    assert osp_reloaded.company.name == "OSP Untouched", (
        "Apply in Test Client must not modify OSP icp_config"
    )


# ---------------------------------------------------------------------------
# S8. Apply in OSP does not change Test Client
# ---------------------------------------------------------------------------

def test_apply_in_osp_does_not_change_test_client():
    osp_id = _seed_osp()
    save_workspace_icp_config(_cfg("OSP Before"), workspace_id=osp_id)

    client_ws = create_workspace(
        name="Test Client S8", slug="test-client-s8",
        instantly_campaign_id="camp-s8",
    )
    client_id = client_ws["id"]
    save_workspace_icp_config(_cfg("Client Untouched"), workspace_id=client_id)

    sugg = _suggestions(company_name="OSP After")
    osp_cfg = load_workspace_icp_config(workspace_id=osp_id)
    updated = apply_suggestions_to_config(osp_cfg, sugg, {"company_name"})
    save_workspace_icp_config(updated, workspace_id=osp_id)

    client_reloaded = load_workspace_icp_config(workspace_id=client_id)
    osp_reloaded = load_workspace_icp_config(workspace_id=osp_id)

    assert osp_reloaded.company.name == "OSP After"
    assert client_reloaded.company.name == "Client Untouched", (
        "Apply in OSP must not modify Test Client icp_config"
    )


# ---------------------------------------------------------------------------
# S9. Prompt configs are not touched by apply
# ---------------------------------------------------------------------------

def test_prompt_configs_not_touched_by_apply():
    ws_id = _seed_osp()

    with session_scope() as s:
        before = s.execute(select(func.count()).select_from(PromptConfig)).scalar()

    sugg = _suggestions()
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    updated = apply_suggestions_to_config(cfg, sugg, set(WebsiteSuggestions.model_fields.keys()))
    save_workspace_icp_config(updated, workspace_id=ws_id)

    with session_scope() as s:
        after = s.execute(select(func.count()).select_from(PromptConfig)).scalar()

    assert after == before, "apply + save must not create or modify PromptConfig rows"


# ---------------------------------------------------------------------------
# S10. After save, loading settings (fresh session) returns new values
# ---------------------------------------------------------------------------

def test_fresh_load_after_save_returns_new_values():
    ws_id = _seed_osp()
    save_workspace_icp_config(_cfg("Stale"), workspace_id=ws_id)

    sugg = _suggestions(
        company_name="Fresh",
        value_props=["prop A", "prop B"],
        target_industries=["SaaS"],
    )
    cfg = load_workspace_icp_config(workspace_id=ws_id)
    updated = apply_suggestions_to_config(cfg, sugg, {"company_name", "value_props", "target_industries"})
    save_workspace_icp_config(updated, workspace_id=ws_id)

    # Simulate the Settings page re-loading in a fresh DB session (new session_scope)
    fresh = load_workspace_icp_config(workspace_id=ws_id)
    assert fresh.company.name == "Fresh"
    assert fresh.company.value_props == ["prop A", "prop B"]
    assert "SaaS" in fresh.icp.target_industries
