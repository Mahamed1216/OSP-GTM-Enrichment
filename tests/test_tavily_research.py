"""Tavily Research agent (Company Researcher) in buyer/company research.

Research runs as a layer of discover_buyer_accounts (before LLM classification),
its output is stored on BuyerAccountResult, surfaced on Lead Detail, and gated
so scoring/content never invent a signal when none was found. No live HTTP/LLM.
"""
from __future__ import annotations

import asyncio

import src.enrichment.buyer_accounts as bac
from src.enrichment.buyer_accounts import BuyerAccountResult, discover_buyer_accounts
from src.icp_config import ICPConfig, load_workspace_icp_config, save_workspace_icp_config
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace


def _install(monkeypatch, *, research_resp="default", research_raises=False, llm_useful=True):
    """Patch all Tavily layers + LLM. Returns the recorded research inputs."""
    research_inputs: list[str] = []

    def fake_search_sync(query, max_results, *, start_date=None, end_date=None,
                         topic=None, search_depth="basic"):
        if start_date:
            return [{"title": "sig", "content": "x", "url": "https://e.com/s"}]
        return [{"title": "cust", "content": "Beta", "url": "https://e.com/c"}]

    def fake_crawl_sync(url, *, max_depth=1, limit=15):
        return [{"url": "https://e.com/about", "raw_content": "about"}]

    def fake_extract_sync(urls):
        return [{"url": urls[0], "raw_content": "x"}] if urls else []

    def fake_research_sync(input_text, *, model="mini", timeout=60.0):
        research_inputs.append(input_text)
        if research_raises:
            raise RuntimeError("research API down")
        if research_resp == "default":
            return {
                "status": "completed",
                "answer": "Acme raised Series B ~45 days ago and is hiring RevOps.",
                "sources": [{"url": "https://news.example.com/acme"}],
            }
        return research_resp

    async def fake_gen(*, model, system, user, schema, max_tokens):
        return BuyerAccountResult(
            buyer_motion="B2B",
            direct_buyer_accounts=["Beta", "Gamma"],
            buyer_confidence="medium",
            research_useful_signal_found=bool(llm_useful),
            research_no_signal_reason="" if llm_useful else "summary too generic",
        )

    monkeypatch.setattr(bac, "_tavily_search_sync", fake_search_sync)
    monkeypatch.setattr(bac, "_crawl_sync", fake_crawl_sync)
    monkeypatch.setattr(bac, "_extract_sync", fake_extract_sync)
    monkeypatch.setattr(bac, "_research_sync", fake_research_sync)
    monkeypatch.setattr(bac, "generate_json", fake_gen)
    monkeypatch.setattr(bac.settings, "tavily_api_key", "x")
    monkeypatch.setattr(bac.settings, "anthropic_api_key", "x")
    return research_inputs


def _run(**kw):
    return asyncio.run(discover_buyer_accounts(
        "Acme", company_domain="acme.com", news_window_days=90, **kw
    ))


# ---------------------------------------------------------------------------
# 1 & 2. Research runs when enabled, skipped when disabled
# ---------------------------------------------------------------------------

def test_research_runs_when_enabled(monkeypatch):
    inputs = _install(monkeypatch)
    result = _run(use_research=True)
    assert inputs, "research was not called"
    assert result.research_used is True
    assert result.research_status == "completed"
    assert "research" in result.tavily_modes_used


def test_research_skipped_when_disabled(monkeypatch):
    inputs = _install(monkeypatch)
    result = _run(use_research=False)
    assert inputs == []  # research never called
    assert result.research_used is False
    assert result.research_status == "skipped"
    assert "research" not in result.tavily_modes_used


# ---------------------------------------------------------------------------
# 3. Research mode appears in debug metadata (with params)
# ---------------------------------------------------------------------------

def test_research_in_debug_metadata(monkeypatch):
    _install(monkeypatch)
    result = _run(use_research=True)
    research_calls = [c for c in result.tavily_calls if c.get("mode") == "research"]
    assert research_calls
    c = research_calls[0]
    assert c["topic"] == "research"
    assert c["start_date"] and c["end_date"]
    assert c["window_days"] == 90
    assert c["status"] == "completed"
    assert "source_count" in c


# ---------------------------------------------------------------------------
# 4. Research output stored in buyer account research JSON
# ---------------------------------------------------------------------------

def test_research_output_stored(monkeypatch):
    _install(monkeypatch)
    result = _run(use_research=True)
    d = result.model_dump()
    assert d["research_used"] is True
    assert "Series B" in d["research_summary"]
    assert d["research_sources"] == ["https://news.example.com/acme"]
    assert d["research_raw_payload"]["status"] == "completed"
    assert d["research_last_run_at"]


# ---------------------------------------------------------------------------
# 6. Completed-but-not-useful path
# ---------------------------------------------------------------------------

def test_completed_but_no_useful_signal(monkeypatch):
    _install(monkeypatch, llm_useful=False)
    result = _run(use_research=True)
    assert result.research_status == "completed"
    assert result.research_useful_signal_found is False
    assert result.research_no_signal_reason  # has a reason


def test_empty_research_summary_marks_not_useful(monkeypatch):
    _install(monkeypatch, research_resp={"status": "completed"})  # no answer/summary
    result = _run(use_research=True)
    assert result.research_useful_signal_found is False
    assert result.research_no_signal_reason


# ---------------------------------------------------------------------------
# 8. Research failure stores failed status + error
# ---------------------------------------------------------------------------

def test_research_failure_recorded(monkeypatch):
    _install(monkeypatch, research_raises=True)
    result = _run(use_research=True)
    assert result.research_status == "failed"
    assert result.research_error and "research API down" in result.research_error
    assert result.research_useful_signal_found is False
    failed_calls = [c for c in result.tavily_calls if c.get("mode") == "research"]
    assert failed_calls and failed_calls[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# 5 & 7. Lead Detail data path (completed / not run)
# ---------------------------------------------------------------------------

def test_lead_detail_data_completed(monkeypatch):
    _install(monkeypatch)
    result = _run(use_research=True)
    # The buyer_accounts dict the Lead Detail page renders carries the fields.
    d = result.model_dump()
    assert d["research_used"] and d["research_status"] == "completed"
    assert d["research_useful_signal_found"] is True
    assert d["research_summary"]


def test_lead_detail_data_not_run(monkeypatch):
    _install(monkeypatch)
    result = _run(use_research=False)
    d = result.model_dump()
    assert d["research_used"] is False  # UI shows "has not run for this lead"


# ---------------------------------------------------------------------------
# 9 & 10. Scoring uses useful research; never invents when none
# ---------------------------------------------------------------------------

def test_context_includes_useful_research():
    from src.context import format_lead_context
    from src.models import Enrichment, Lead

    lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Acme")
    enrichment = Enrichment(lead_id=1, buyer_accounts={
        "buyer_motion": "B2B",
        "research_used": True,
        "research_useful_signal_found": True,
        "research_summary": "Acme raised Series B and is hiring RevOps.",
    })
    out = format_lead_context(lead, enrichment, None)
    assert "Series B and is hiring RevOps" in out


def test_context_no_invented_signal_when_research_not_useful():
    from src.context import format_lead_context
    from src.models import Enrichment, Lead

    lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Acme")
    enrichment = Enrichment(lead_id=1, buyer_accounts={
        "buyer_motion": "B2B",
        "research_used": True,
        "research_useful_signal_found": False,
        "research_summary": "Acme raised Series B.",  # present but flagged not useful
    })
    out = format_lead_context(lead, enrichment, None)
    assert "No useful Tavily company research signal found" in out
    assert "Series B" not in out  # the un-useful summary is NOT surfaced as evidence


# ---------------------------------------------------------------------------
# 11 & 12. Content gating (same context fn the email generator uses)
# ---------------------------------------------------------------------------

def test_content_uses_research_angle_only_when_useful():
    from src.context import format_lead_context
    from src.models import Enrichment, Lead

    lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Acme")
    useful = Enrichment(lead_id=1, buyer_accounts={
        "research_used": True, "research_useful_signal_found": True,
        "research_summary": "Hiring 5 RevOps roles after Series B.",
    })
    not_useful = Enrichment(lead_id=2, buyer_accounts={
        "research_used": True, "research_useful_signal_found": False,
        "research_summary": "Hiring 5 RevOps roles after Series B.",
    })
    assert "RevOps roles after Series B" in format_lead_context(lead, useful, None)
    assert "RevOps roles after Series B" not in format_lead_context(lead, not_useful, None)


# ---------------------------------------------------------------------------
# 13. Workspace A research setting does not affect Workspace B
# ---------------------------------------------------------------------------

def test_research_setting_workspace_isolated():
    seed_default_workspace()
    osp = get_default_workspace_id()
    other = create_workspace(name="WS B", slug="ws-b", instantly_campaign_id="c-b")["id"]
    save_workspace_icp_config(ICPConfig(buyer_research_use_research=True), workspace_id=osp)
    save_workspace_icp_config(ICPConfig(buyer_research_use_research=False), workspace_id=other)
    assert load_workspace_icp_config(osp).buyer_research_use_research is True
    assert load_workspace_icp_config(other).buyer_research_use_research is False


# ---------------------------------------------------------------------------
# 14 & 15. Research never pushes to Instantly or sends email
# ---------------------------------------------------------------------------

def test_research_does_not_push_or_send(monkeypatch):
    _install(monkeypatch)
    import src.delivery.instantly as inst
    pushed = {"called": False}

    def _boom(*a, **k):
        pushed["called"] = True
        raise AssertionError("research must never push/send")

    monkeypatch.setattr(inst, "deliver_email", _boom, raising=False)
    result = _run(use_research=True)
    assert isinstance(result, BuyerAccountResult)
    assert pushed["called"] is False


# ---------------------------------------------------------------------------
# 16. Call script + LinkedIn DM generation remain disabled by default
# ---------------------------------------------------------------------------

def test_call_script_and_dm_disabled_by_default():
    c = ICPConfig()
    assert c.generate_call_script_enabled is False
    assert c.generate_linkedin_dm_enabled is False
    assert c.generate_email_enabled is True
    assert c.buyer_research_use_research is True  # research default on
