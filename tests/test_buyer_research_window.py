"""Layered buyer/company research: 90-day Tavily Search + Crawl + Extract.

Buyer research bounds its date-sensitive company-signal searches to the last N
days (default 90) and layers news + general search, company-website crawl, and
extract of the strongest URLs — selecting the strongest RELEVANT signal over
the freshest one. No live HTTP/LLM: the Tavily sync calls and the LLM are
patched.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import src.enrichment.buyer_accounts as bac
from src.enrichment.buyer_accounts import (
    BuyerAccountResult,
    CandidateSignal,
    compute_news_window,
    discover_buyer_accounts,
    select_strongest_signal,
)
from src.icp_config import ICPConfig, load_workspace_icp_config, save_workspace_icp_config
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace


def _install_fakes(monkeypatch, *, news_results="default", general_results="default"):
    search_calls: list[dict] = []
    crawl_calls: list[dict] = []
    extract_calls: list[dict] = []
    captured: dict = {}

    def fake_search_sync(query, max_results, *, start_date=None, end_date=None,
                         topic=None, search_depth="basic"):
        search_calls.append({
            "query": query, "max_results": max_results, "start_date": start_date,
            "end_date": end_date, "topic": topic, "search_depth": search_depth,
        })
        if start_date and topic == "news":
            if news_results == "default":
                return [{"title": "Acme Series B", "content": "raised $40M",
                         "url": "https://news.example.com/acme", "published_date": "2026-05-01"}]
            return news_results
        if start_date and topic == "general":
            if general_results == "default":
                return [{"title": "Acme careers", "content": "hiring RevOps",
                         "url": "https://acme.example.com/careers", "published_date": "2026-04-20"}]
            return general_results
        # Un-windowed evidence query (customers / case studies / what they sell).
        return [{"title": "Acme customers", "content": "Beta uses Acme",
                 "url": "https://acme.example.com/customers"}]

    def fake_crawl_sync(url, *, max_depth=1, limit=15):
        crawl_calls.append({"url": url, "max_depth": max_depth, "limit": limit})
        return [{"url": "https://acme.example.com/about", "raw_content": "What Acme does"}]

    def fake_extract_sync(urls):
        extract_calls.append({"urls": list(urls)})
        return [{"url": urls[0], "raw_content": "Extracted detail"}] if urls else []

    async def fake_gen(*, model, system, user, schema, max_tokens):
        captured["system"] = system
        captured["user"] = user
        return BuyerAccountResult(
            buyer_motion="B2B",
            likely_direct_buyers=["Beta Corp"],
            buyer_confidence="medium",
            direct_buyer_accounts=["Beta Corp", "Gamma Inc"],
            reasoning="Funding + RevOps hiring ~45 days ago.",
            company_context_summary="Acme sells RevOps tooling to B2B SaaS.",
            recommended_outreach_angle="Saw the Series B + RevOps hiring.",
            candidate_signals=[
                CandidateSignal(signal_type="funding", signal_summary="Series B",
                                signal_relevance="high", signal_recency_days=45,
                                usable_for_scoring=True, usable_for_email=True,
                                source_url="https://news.example.com/acme"),
                CandidateSignal(signal_type="blog", signal_summary="generic post",
                                signal_relevance="none", signal_recency_days=4),
            ],
        )

    monkeypatch.setattr(bac, "_tavily_search_sync", fake_search_sync)
    monkeypatch.setattr(bac, "_crawl_sync", fake_crawl_sync)
    monkeypatch.setattr(bac, "_extract_sync", fake_extract_sync)
    monkeypatch.setattr(bac, "generate_json", fake_gen)
    monkeypatch.setattr(bac.settings, "tavily_api_key", "tvly-test")
    monkeypatch.setattr(bac.settings, "anthropic_api_key", "anthropic-test")
    return search_calls, crawl_calls, extract_calls, captured


def _run(**kw):
    return asyncio.run(discover_buyer_accounts(
        "Acme", company_domain="acme.com", news_window_days=90, **kw
    ))


# ---------------------------------------------------------------------------
# 1 & 3 & 4. Date-sensitive Search calls receive the configured 90-day window
# ---------------------------------------------------------------------------

def test_news_and_general_signal_searches_are_windowed(monkeypatch):
    search_calls, *_ = _install_fakes(monkeypatch)
    _run()
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=90)).isoformat()

    news = [c for c in search_calls if c["topic"] == "news" and c["start_date"]]
    gen_windowed = [c for c in search_calls if c["topic"] == "general" and c["start_date"]]
    assert news and news[0]["start_date"] == start and news[0]["end_date"] == today.isoformat()
    assert gen_windowed and gen_windowed[0]["start_date"] == start

    # Evidence queries (customers/case studies) are intentionally NOT windowed.
    evidence = [c for c in search_calls if "customers" in c["query"]]
    assert evidence and evidence[0]["start_date"] is None


def test_compute_news_window_today_minus_90():
    start, end = compute_news_window(90)
    today = datetime.now(timezone.utc).date()
    assert end == today.isoformat()
    assert start == (today - timedelta(days=90)).isoformat()


def test_default_window_is_90():
    assert ICPConfig().buyer_research_news_window_days == 90


# ---------------------------------------------------------------------------
# 2. No buyer research call uses time_range day/week
# ---------------------------------------------------------------------------

def test_no_call_uses_time_range(monkeypatch):
    search_calls, *_ = _install_fakes(monkeypatch)
    _run()
    for c in search_calls:
        assert "time_range" not in c  # wrapper never sets time_range


# ---------------------------------------------------------------------------
# 5. Company crawl runs when website exists and the setting is enabled
# ---------------------------------------------------------------------------

def test_crawl_runs_when_enabled_and_domain_present(monkeypatch):
    _, crawl_calls, _, _ = _install_fakes(monkeypatch)
    _run(use_crawl=True)
    assert crawl_calls and crawl_calls[0]["url"] == "https://acme.com"


def test_crawl_skipped_when_disabled(monkeypatch):
    _, crawl_calls, _, _ = _install_fakes(monkeypatch)
    _run(use_crawl=False)
    assert crawl_calls == []


# ---------------------------------------------------------------------------
# 6. Extract runs on top URLs when enabled
# ---------------------------------------------------------------------------

def test_extract_runs_on_top_urls_when_enabled(monkeypatch):
    _, _, extract_calls, _ = _install_fakes(monkeypatch)
    _run(use_extract=True)
    assert extract_calls
    assert 1 <= len(extract_calls[0]["urls"]) <= 5


def test_extract_skipped_when_disabled(monkeypatch):
    _, _, extract_calls, _ = _install_fakes(monkeypatch)
    _run(use_extract=False)
    assert extract_calls == []


# ---------------------------------------------------------------------------
# 7. Weak/empty news → general + crawl + extract still run
# ---------------------------------------------------------------------------

def test_fallback_runs_when_news_empty(monkeypatch):
    search_calls, crawl_calls, extract_calls, _ = _install_fakes(
        monkeypatch, news_results=[]
    )
    result = _run()
    # General windowed signal search still ran...
    gen_windowed = [c for c in search_calls if c["topic"] == "general" and c["start_date"]]
    assert gen_windowed
    # ...and crawl + extract still ran despite weak news.
    assert crawl_calls
    assert extract_calls
    assert "general" in result.tavily_modes_used


# ---------------------------------------------------------------------------
# 8. Ranking prefers a relevant 60-day signal over an irrelevant 4-day one
# ---------------------------------------------------------------------------

def test_select_strongest_prefers_relevance_over_recency():
    relevant_old = CandidateSignal(signal_type="hiring", signal_relevance="high",
                                   signal_recency_days=60, usable_for_scoring=True)
    irrelevant_new = CandidateSignal(signal_type="blog", signal_relevance="none",
                                     signal_recency_days=4)
    chosen = select_strongest_signal([irrelevant_new, relevant_old])
    assert chosen is relevant_old


def test_select_strongest_recency_breaks_ties():
    older = CandidateSignal(signal_type="funding", signal_relevance="high", signal_recency_days=80)
    newer = CandidateSignal(signal_type="hiring", signal_relevance="high", signal_recency_days=20)
    assert select_strongest_signal([older, newer]) is newer


def test_select_strongest_none_when_all_irrelevant():
    assert select_strongest_signal([
        CandidateSignal(signal_relevance="none", signal_recency_days=2),
    ]) is None


# ---------------------------------------------------------------------------
# 9 & 10. Metadata stores all Tavily calls + params (Lead Detail can display)
# ---------------------------------------------------------------------------

def test_metadata_records_all_tavily_calls(monkeypatch):
    _install_fakes(monkeypatch)
    result = _run()
    assert result.news_window_days == 90
    assert result.tavily_calls, "tavily_calls debug metadata missing"
    # Modes used should include the four layers.
    for mode in ("news", "general", "crawl", "extract"):
        assert mode in result.tavily_modes_used, f"{mode} missing from modes_used"
    # Each call record carries the params needed by the Lead Detail debug panel.
    one = result.tavily_calls[0]
    for key in ("mode", "topic", "search_depth", "max_results",
                "start_date", "end_date", "time_range", "result_count", "source_urls"):
        assert key in one
    # Selected strongest signal is the relevant one (relevance-first).
    assert result.selected_signal is not None
    assert result.selected_signal.signal_type == "funding"
    assert result.selected_signal.signal_relevance == "high"


def test_ranking_instruction_in_prompt(monkeypatch):
    _, _, _, captured = _install_fakes(monkeypatch)
    _run()
    assert "RANK BY RELEVANCE FIRST" in captured["user"].upper()


# ---------------------------------------------------------------------------
# 11. Workspace A buyer research settings do not affect Workspace B
# ---------------------------------------------------------------------------

def test_buyer_research_settings_workspace_isolated():
    seed_default_workspace()
    osp = get_default_workspace_id()
    other = create_workspace(name="WS B", slug="ws-b", instantly_campaign_id="c-b")["id"]
    save_workspace_icp_config(
        ICPConfig(buyer_research_news_window_days=120, buyer_research_use_crawl=True),
        workspace_id=osp,
    )
    save_workspace_icp_config(
        ICPConfig(buyer_research_news_window_days=30, buyer_research_use_crawl=False),
        workspace_id=other,
    )
    a = load_workspace_icp_config(osp)
    b = load_workspace_icp_config(other)
    assert a.buyer_research_news_window_days == 120 and a.buyer_research_use_crawl is True
    assert b.buyer_research_news_window_days == 30 and b.buyer_research_use_crawl is False


# ---------------------------------------------------------------------------
# 12. Scoring context can use the selected strongest relevant signal
# ---------------------------------------------------------------------------

def test_scoring_context_includes_selected_signal():
    from src.context import format_lead_context
    from src.models import Enrichment, Lead

    lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Acme", industry="SaaS")
    enrichment = Enrichment(
        lead_id=1,
        buyer_accounts={
            "buyer_motion": "B2B",
            "likely_direct_buyers": ["Beta Corp"],
            "reasoning": "Funding + RevOps hiring expansion ~45 days ago.",
            "news_window_days": 90,
        },
    )
    out = format_lead_context(lead, enrichment, None)
    assert "RevOps hiring expansion" in out


# ---------------------------------------------------------------------------
# 13 & 14. Buyer research never pushes to Instantly or sends email
# ---------------------------------------------------------------------------

def test_buyer_research_does_not_push_or_send(monkeypatch):
    _install_fakes(monkeypatch)
    import src.delivery.instantly as inst
    pushed = {"called": False}

    def _boom(*a, **k):
        pushed["called"] = True
        raise AssertionError("buyer research must never push/send")

    monkeypatch.setattr(inst, "deliver_email", _boom, raising=False)
    result = _run()
    assert isinstance(result, BuyerAccountResult)
    assert pushed["called"] is False


# ---------------------------------------------------------------------------
# 15. Call script + LinkedIn DM generation remain disabled by default
# ---------------------------------------------------------------------------

def test_call_script_and_dm_disabled_by_default():
    c = ICPConfig()
    assert c.generate_call_script_enabled is False
    assert c.generate_linkedin_dm_enabled is False
    assert c.generate_email_enabled is True
