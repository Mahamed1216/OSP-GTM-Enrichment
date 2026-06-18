"""Buyer-research 90-day Tavily news window (hotfix).

Buyer research now bounds its company-signals search to the last N days
(default 90) via Tavily start_date/end_date instead of surfacing only the last
few days. No live HTTP/LLM — the Tavily sync call and the LLM are patched.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import src.enrichment.buyer_accounts as bac
from src.enrichment.buyer_accounts import (
    BuyerAccountResult,
    compute_news_window,
    discover_buyer_accounts,
)
from src.icp_config import ICPConfig, load_workspace_icp_config, save_workspace_icp_config
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace


def _install_fakes(monkeypatch, *, news_results=None):
    """Patch Tavily + LLM + API keys; return the list that records search calls."""
    calls: list[dict] = []

    def fake_sync(query, max_results, *, start_date=None, end_date=None,
                  topic=None, search_depth="basic"):
        calls.append({
            "query": query, "max_results": max_results,
            "start_date": start_date, "end_date": end_date,
            "topic": topic, "search_depth": search_depth,
        })
        # News query (date-windowed) returns a relevant signal; others return
        # a buyer-evidence row so any_results is True.
        if start_date:
            return news_results if news_results is not None else [
                {"title": "Acme raises $40M Series B",
                 "content": "Acme announced a funding round and RevOps hiring.",
                 "url": "https://news.example.com/acme-funding"},
            ]
        return [{"title": "Acme customers", "content": "Beta Corp uses Acme.",
                 "url": "https://acme.example.com/customers"}]

    captured: dict = {}

    async def fake_gen(*, model, system, user, schema, max_tokens):
        captured["system"] = system
        captured["user"] = user
        return BuyerAccountResult(
            buyer_motion="B2B",
            likely_direct_buyers=["Beta Corp"],
            buyer_confidence="medium",
            direct_buyer_accounts=["Beta Corp", "Gamma Inc"],
            reasoning="Funding + RevOps hiring expansion ~45 days ago.",
        )

    monkeypatch.setattr(bac, "_tavily_search_sync", fake_sync)
    monkeypatch.setattr(bac, "generate_json", fake_gen)
    monkeypatch.setattr(bac.settings, "tavily_api_key", "tvly-test")
    monkeypatch.setattr(bac.settings, "anthropic_api_key", "anthropic-test")
    return calls, captured


# ---------------------------------------------------------------------------
# 1. Default news window is 90 days
# ---------------------------------------------------------------------------

def test_default_news_window_is_90():
    assert ICPConfig().buyer_research_news_window_days == 90


# ---------------------------------------------------------------------------
# 3. compute_news_window: start_date is today minus N days (UTC)
# ---------------------------------------------------------------------------

def test_compute_news_window_today_minus_90():
    start, end = compute_news_window(90)
    today = datetime.now(timezone.utc).date()
    assert end == today.isoformat()
    assert start == (today - timedelta(days=90)).isoformat()


# ---------------------------------------------------------------------------
# 2 & 4. The windowed buyer-research call passes start_date/end_date, never time_range
# ---------------------------------------------------------------------------

def test_buyer_research_call_receives_date_window(monkeypatch):
    calls, _ = _install_fakes(monkeypatch)
    asyncio.run(discover_buyer_accounts("Acme", news_window_days=90))

    windowed = [c for c in calls if c["start_date"] and c["end_date"]]
    assert windowed, "no date-windowed Tavily call was made"
    today = datetime.now(timezone.utc).date()
    assert windowed[0]["start_date"] == (today - timedelta(days=90)).isoformat()
    assert windowed[0]["end_date"] == today.isoformat()
    assert windowed[0]["topic"] == "news"


def test_buyer_research_never_uses_time_range(monkeypatch):
    calls, _ = _install_fakes(monkeypatch)
    asyncio.run(discover_buyer_accounts("Acme", news_window_days=90))
    for c in calls:
        assert "time_range" not in c  # our wrapper never sets time_range=day/week


# ---------------------------------------------------------------------------
# 5. Query wording references the configured window, not "today / this week"
# ---------------------------------------------------------------------------

def test_query_wording_says_last_90_days(monkeypatch):
    calls, _ = _install_fakes(monkeypatch)
    asyncio.run(discover_buyer_accounts("Acme", news_window_days=90))
    news_queries = [c["query"] for c in calls if c["start_date"]]
    assert news_queries
    q = news_queries[0].lower()
    assert "last 90 days" in q
    assert "this week" not in q
    assert "today" not in q


def test_query_wording_uses_configured_window(monkeypatch):
    calls, _ = _install_fakes(monkeypatch)
    asyncio.run(discover_buyer_accounts("Acme", news_window_days=60))
    news_queries = [c["query"] for c in calls if c["start_date"]]
    assert any("last 60 days" in q.lower() for q in news_queries)


# ---------------------------------------------------------------------------
# 6. Ranking instruction prefers relevance over mere recency
# ---------------------------------------------------------------------------

def test_ranking_rule_relevance_over_recency(monkeypatch):
    _, captured = _install_fakes(monkeypatch)
    asyncio.run(discover_buyer_accounts("Acme", news_window_days=90))
    system = captured["system"].upper()
    user = captured["user"].lower()
    # System prompt instructs relevance-first, then recency.
    assert "RANK BY RELEVANCE FIRST" in system
    # User message reinforces it for this lead.
    assert "relevance first" in user and "recency" in user


# ---------------------------------------------------------------------------
# 7. Research metadata is stored on the result
# ---------------------------------------------------------------------------

def test_research_metadata_stored(monkeypatch):
    _install_fakes(monkeypatch)
    result = asyncio.run(discover_buyer_accounts("Acme", news_window_days=90))
    today = datetime.now(timezone.utc).date()
    assert result.news_window_days == 90
    assert result.news_start_date == (today - timedelta(days=90)).isoformat()
    assert result.news_end_date == today.isoformat()
    assert result.tavily_topic_used == "news"
    assert result.result_count > 0
    assert any("example.com" in u for u in result.source_urls)


def test_topic_falls_back_to_general_when_news_empty(monkeypatch):
    # News topic returns nothing → fallback to general (same date window).
    calls: list[dict] = []

    def fake_sync(query, max_results, *, start_date=None, end_date=None,
                  topic=None, search_depth="basic"):
        calls.append({"topic": topic, "start_date": start_date})
        if start_date and topic == "news":
            return []  # news topic empty
        if start_date and topic == "general":
            return [{"title": "Acme expands", "content": "new office", "url": "https://e.com/x"}]
        return [{"title": "cust", "content": "Beta", "url": "https://e.com/c"}]

    async def fake_gen(*, model, system, user, schema, max_tokens):
        return BuyerAccountResult(buyer_motion="B2B")

    monkeypatch.setattr(bac, "_tavily_search_sync", fake_sync)
    monkeypatch.setattr(bac, "generate_json", fake_gen)
    monkeypatch.setattr(bac.settings, "tavily_api_key", "x")
    monkeypatch.setattr(bac.settings, "anthropic_api_key", "x")

    result = asyncio.run(discover_buyer_accounts("Acme", news_window_days=90))
    assert result.tavily_topic_used == "general"
    # Both a news AND a general windowed call were attempted.
    topics = [c["topic"] for c in calls if c["start_date"]]
    assert "news" in topics and "general" in topics


# ---------------------------------------------------------------------------
# 8. Workspace A news window does not affect Workspace B
# ---------------------------------------------------------------------------

def test_news_window_workspace_isolated():
    seed_default_workspace()
    osp = get_default_workspace_id()
    other = create_workspace(name="WS B", slug="ws-b", instantly_campaign_id="c-b")["id"]

    save_workspace_icp_config(ICPConfig(buyer_research_news_window_days=120), workspace_id=osp)
    save_workspace_icp_config(ICPConfig(buyer_research_news_window_days=30), workspace_id=other)

    assert load_workspace_icp_config(osp).buyer_research_news_window_days == 120
    assert load_workspace_icp_config(other).buyer_research_news_window_days == 30


# ---------------------------------------------------------------------------
# 9. Scoring context can use a relevant 90-day signal
# ---------------------------------------------------------------------------

def test_scoring_context_includes_90day_signal():
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
    assert "RevOps hiring expansion" in out  # the 45-day-old signal is available


# ---------------------------------------------------------------------------
# 10. Buyer research never pushes to Instantly or sends email
# ---------------------------------------------------------------------------

def test_buyer_research_does_not_push_or_send(monkeypatch):
    _install_fakes(monkeypatch)

    import src.delivery.instantly as inst
    pushed = {"called": False}

    def _boom(*a, **k):
        pushed["called"] = True
        raise AssertionError("buyer research must never push/send")

    monkeypatch.setattr(inst, "deliver_email", _boom, raising=False)

    result = asyncio.run(discover_buyer_accounts("Acme", news_window_days=90))
    assert isinstance(result, BuyerAccountResult)
    assert pushed["called"] is False
