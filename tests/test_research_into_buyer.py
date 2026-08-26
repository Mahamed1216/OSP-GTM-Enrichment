"""Tavily Company Research flows into Buyer Account Research — Issue 1 hotfix.

Research findings must improve buyer-segmentation fields (not only scoring),
expose a visible 'company research signal used' flag, and never invent named
buyers or competitors. No live HTTP/LLM — Tavily layers + LLM are patched.
"""
from __future__ import annotations

import asyncio

import src.enrichment.buyer_accounts as bac
from src.enrichment.buyer_accounts import BuyerAccountResult, discover_buyer_accounts


def _install(monkeypatch, *, research_useful=True, llm=None):
    captured: dict = {}

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
        return {
            "status": "completed",
            "answer": ("Keychain runs a manufacturing marketplace serving CPG "
                       "brands and regional grocery retailers."),
            "sources": [{"url": "https://news.example.com/keychain"}],
        }

    async def fake_gen(*, model, system, user, schema, max_tokens):
        captured["system"] = system
        captured["user"] = user
        if llm is not None:
            return llm
        return BuyerAccountResult(
            buyer_motion="B2B",
            buyer_confidence="low",
            likely_buyer_segments=[
                "CPG brands sourcing private-label manufacturing",
                "regional grocery retailers expanding store-brand portfolios",
            ],
            trigger_based_buyer_segments=[
                "CPG brands launching new private-label SKUs",
            ],
            research_useful_signal_found=research_useful,
            company_research_signal_used=research_useful,
            company_research_effect=(
                "Grounded buyer segments in the marketplace's CPG + retailer focus."
                if research_useful else ""
            ),
            buyer_research_rationale=(
                "Company research confirmed Keychain targets CPG brands and "
                "retailers; no named confirmed buyers found, used research-backed "
                "segments instead." if research_useful else "No useful research."
            ),
        )

    monkeypatch.setattr(bac, "_tavily_search_sync", fake_search_sync)
    monkeypatch.setattr(bac, "_crawl_sync", fake_crawl_sync)
    monkeypatch.setattr(bac, "_extract_sync", fake_extract_sync)
    monkeypatch.setattr(bac, "_research_sync", fake_research_sync)
    monkeypatch.setattr(bac, "generate_json", fake_gen)
    monkeypatch.setattr(bac.settings, "tavily_api_key", "x")
    monkeypatch.setattr(bac.settings, "anthropic_api_key", "x")
    return captured


def _run(**kw):
    return asyncio.run(discover_buyer_accounts(
        "Keychain", company_domain="keychain.com", news_window_days=90, **kw
    ))


# ---------------------------------------------------------------------------
# 1. Useful research is passed INTO buyer research classification
# ---------------------------------------------------------------------------

def test_research_summary_passed_into_classification(monkeypatch):
    captured = _install(monkeypatch)
    _run()
    user = captured["user"]
    assert "Tavily company research (agent summary)" in user
    assert "manufacturing marketplace" in user  # the actual research text


def test_prompt_instructs_using_research_for_segments(monkeypatch):
    captured = _install(monkeypatch)
    _run()
    user = captured["user"]
    assert "USE the research to IMPROVE buyer segmentation" in user
    # Specific vs vague guidance + safety rules present.
    assert "private-label manufacturing" in user
    assert "businesses looking to grow" in user  # the banned-vague example
    assert "never list a competitor as a buyer" in user
    assert "CONFIRMS it as a real customer" in user  # don't invent named buyers
    assert "consistent with the research signal" in user


# ---------------------------------------------------------------------------
# 2 & 3. Research improves trigger/likely buyer segments
# ---------------------------------------------------------------------------

def test_research_improves_segments(monkeypatch):
    _install(monkeypatch)
    result = _run()
    assert any("private-label" in s for s in result.likely_buyer_segments)
    assert any("private-label" in s for s in result.trigger_based_buyer_segments)


# ---------------------------------------------------------------------------
# 4. Buyer research rationale mentions company research when used
# ---------------------------------------------------------------------------

def test_rationale_mentions_company_research(monkeypatch):
    _install(monkeypatch)
    result = _run()
    assert "company research" in result.buyer_research_rationale.lower()


# ---------------------------------------------------------------------------
# 5. company_research_signal_used = True surfaces (UI shows yes)
# ---------------------------------------------------------------------------

def test_company_research_signal_used_true(monkeypatch):
    _install(monkeypatch, research_useful=True)
    result = _run()
    assert result.company_research_signal_used is True
    assert result.company_research_effect


# ---------------------------------------------------------------------------
# 6. No useful research → UI clearly says it was not used
# ---------------------------------------------------------------------------

def test_company_research_not_used_when_not_useful(monkeypatch):
    _install(monkeypatch, research_useful=False)
    result = _run()
    assert result.company_research_signal_used is False
    assert "did not produce a useful" in result.company_research_effect


# ---------------------------------------------------------------------------
# 7 & 8. Competitors never named as buyers; named buyers not invented
# ---------------------------------------------------------------------------

def test_system_prompt_keeps_competitor_and_invention_safety(monkeypatch):
    captured = _install(monkeypatch)
    _run()
    system = captured["system"]
    # Existing hard rules remain in the system prompt.
    assert "Never list a competitor" in system or "NEVER" in system
    assert "competitor" in system.lower()


def test_llm_can_leave_direct_buyers_empty_with_research(monkeypatch):
    # When research confirms no named accounts, direct_buyer_accounts stays empty
    # and the fallback is segment-based — nothing invented.
    empty_named = BuyerAccountResult(
        buyer_motion="B2B",
        direct_buyer_accounts=[],
        likely_buyer_segments=["CPG brands sourcing private-label manufacturing"],
        trigger_based_buyer_segments=["CPG brands launching private-label SKUs"],
        research_useful_signal_found=True,
        company_research_signal_used=True,
    )
    _install(monkeypatch, llm=empty_named)
    result = _run()
    assert result.direct_buyer_accounts == []
    assert result.buyer_fallback_mode in ("trigger_segment", "needs_review",
                                          "lookalike_accounts")


# ---------------------------------------------------------------------------
# 9. Scoring + buyer rationale consistency (prompt enforces it)
# ---------------------------------------------------------------------------

def test_prompt_enforces_rationale_consistency(monkeypatch):
    captured = _install(monkeypatch)
    _run()
    assert "Mention in buyer_research_rationale whether company research was used" in captured["user"]


# ---------------------------------------------------------------------------
# 10. No Tavily call runs on page load (get_lead_full is read-only)
# ---------------------------------------------------------------------------

def test_no_tavily_on_page_load(monkeypatch):
    from src.lib.db_queries import get_lead_full
    from src.db import session_scope
    from src.models import Enrichment, Lead
    from src.workspace import get_default_workspace_id, seed_default_workspace

    seed_default_workspace()
    ws = get_default_workspace_id()
    with session_scope() as s:
        lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Keychain",
                    workspace_id=ws)
        s.add(lead)
        s.flush()
        lid = lead.id
        s.add(Enrichment(lead_id=lid, workspace_id=ws, buyer_accounts={
            "company_research_signal_used": True,
            "research_used": True, "research_useful_signal_found": True,
        }))

    def _boom(*a, **k):
        raise AssertionError("Tavily must not run on page load")

    monkeypatch.setattr(bac, "_research_sync", _boom)
    monkeypatch.setattr(bac, "_tavily_search_sync", _boom)
    bundle = get_lead_full(lid, workspace_id=ws)
    assert bundle["enrichment"]["buyer_accounts"]["company_research_signal_used"] is True
