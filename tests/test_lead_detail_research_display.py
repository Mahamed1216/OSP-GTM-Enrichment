"""Lead Detail Tavily Company Research visibility (storage → loader → state).

The Streamlit page renders one of four states from the stored buyer_accounts
JSON. These tests cover persistence, the get_lead_full loader returning the
research fields, the pure state selector for each UI state, and that opening
Lead Detail never triggers a Tavily/research call.
"""
from __future__ import annotations

from sqlalchemy import select

from app.lib.db_queries import get_lead_full
from app.lib.research_display import research_display_state
from src.db import session_scope
from src.models import Enrichment, GeneratedContent, Lead
from src.workspace import get_default_workspace_id, seed_default_workspace


def _seed_osp() -> int:
    seed_default_workspace()
    osp = get_default_workspace_id()
    assert osp is not None
    return osp


def _make_lead_with_buyer_accounts(osp: int, ba: dict) -> int:
    with session_scope() as s:
        lead = Lead(first_name="T", last_name="U", email="t@x.com",
                    company="Acme", workspace_id=osp)
        s.add(lead)
        s.flush()
        lid = lead.id
        s.add(Enrichment(lead_id=lid, buyer_accounts=ba, workspace_id=osp))
        return lid


_USEFUL_BA = {
    "buyer_motion": "B2B",
    "research_used": True,
    "research_status": "completed",
    "research_useful_signal_found": True,
    "research_summary": "Acme raised Series B and is hiring RevOps.",
    "research_findings": ["Series B", "RevOps hiring"],
    "research_sources": ["https://news.example.com/acme"],
    "research_last_run_at": "2026-06-18T00:00:00+00:00",
    "selected_signal": {"signal_type": "funding", "signal_relevance": "high",
                        "signal_recency_days": 45, "signal_summary": "Series B",
                        "reason": "fresh funding = budget"},
    "tavily_calls": [
        {"mode": "research", "topic": "research", "start_date": "2026-03-20",
         "end_date": "2026-06-18", "window_days": 90, "status": "completed",
         "source_count": 1, "source_urls": ["https://news.example.com/acme"],
         "query": "Research the company Acme..."},
    ],
    "research_raw_payload": {"status": "completed", "answer": "..."},
}


# ---------------------------------------------------------------------------
# 1. Research output persisted in buyer_accounts JSON (round-trips through DB)
# ---------------------------------------------------------------------------

def test_research_fields_persisted_in_buyer_accounts():
    osp = _seed_osp()
    lid = _make_lead_with_buyer_accounts(osp, _USEFUL_BA)
    with session_scope() as s:
        row = s.execute(select(Enrichment).where(Enrichment.lead_id == lid)).scalar_one()
        ba = row.buyer_accounts
    assert ba["research_used"] is True
    assert ba["research_status"] == "completed"
    assert ba["research_summary"].startswith("Acme raised")
    assert ba["research_sources"] == ["https://news.example.com/acme"]


# ---------------------------------------------------------------------------
# 2. Lead Detail loader (get_lead_full) returns the research fields
# ---------------------------------------------------------------------------

def test_get_lead_full_returns_research_fields():
    osp = _seed_osp()
    lid = _make_lead_with_buyer_accounts(osp, _USEFUL_BA)
    bundle = get_lead_full(lid, workspace_id=osp)
    ba = bundle["enrichment"]["buyer_accounts"]
    for key in ("research_used", "research_status", "research_summary",
                "research_useful_signal_found", "research_sources",
                "research_last_run_at", "tavily_calls"):
        assert key in ba, f"{key} missing from loaded buyer_accounts"


# ---------------------------------------------------------------------------
# 3-6. State selector renders the correct UI state
# ---------------------------------------------------------------------------

def test_state_useful():
    assert research_display_state(_USEFUL_BA) == "useful"


def test_state_no_useful():
    assert research_display_state({
        "research_used": True, "research_status": "completed",
        "research_useful_signal_found": False,
        "research_no_signal_reason": "summary too generic",
    }) == "no_useful"


def test_state_not_run():
    assert research_display_state({}) == "not_run"
    assert research_display_state({"research_used": False}) == "not_run"
    assert research_display_state(None) == "not_run"


def test_state_failed():
    assert research_display_state({
        "research_used": True, "research_status": "failed",
        "research_error": "research API down",
    }) == "failed"


def test_state_skipped_disabled():
    # When /research is disabled, Lead Detail shows a "skipped because disabled"
    # message rather than "has not run".
    assert research_display_state({
        "research_used": False, "research_status": "skipped_disabled",
        "research_no_signal_reason": "Tavily Research is disabled for this workspace.",
    }) == "skipped_disabled"


def test_existing_saved_research_still_displays_when_disabled():
    # A lead enriched earlier (useful research saved) keeps showing that
    # research even though the workspace setting is now off — the saved row
    # carries research_status="completed", not "skipped_disabled".
    osp = _seed_osp()
    lid = _make_lead_with_buyer_accounts(osp, _USEFUL_BA)
    bundle = get_lead_full(lid, workspace_id=osp)
    ba = bundle["enrichment"]["buyer_accounts"]
    assert research_display_state(ba) == "useful"
    assert ba["research_summary"].startswith("Acme raised")


# ---------------------------------------------------------------------------
# 7. Debug metadata includes mode=research (loaded via get_lead_full)
# ---------------------------------------------------------------------------

def test_debug_metadata_includes_research_mode():
    osp = _seed_osp()
    lid = _make_lead_with_buyer_accounts(osp, _USEFUL_BA)
    bundle = get_lead_full(lid, workspace_id=osp)
    calls = bundle["enrichment"]["buyer_accounts"]["tavily_calls"]
    research_calls = [c for c in calls if c.get("mode") == "research"]
    assert research_calls
    assert research_calls[0]["window_days"] == 90
    assert research_calls[0]["start_date"] and research_calls[0]["end_date"]


# ---------------------------------------------------------------------------
# 8. Opening Lead Detail (get_lead_full) never runs a Tavily/research call
# ---------------------------------------------------------------------------

def test_page_load_does_not_run_tavily(monkeypatch):
    osp = _seed_osp()
    lid = _make_lead_with_buyer_accounts(osp, _USEFUL_BA)

    import src.enrichment.buyer_accounts as bac
    called = {"hit": False}

    def _boom(*a, **k):
        called["hit"] = True
        raise AssertionError("Tavily must not run on page load")

    monkeypatch.setattr(bac, "_research_sync", _boom)
    monkeypatch.setattr(bac, "_tavily_search_sync", _boom)
    monkeypatch.setattr(bac, "_crawl_sync", _boom)
    monkeypatch.setattr(bac, "_extract_sync", _boom)

    bundle = get_lead_full(lid, workspace_id=osp)  # simulates page load
    assert bundle["enrichment"]["buyer_accounts"]["research_used"] is True
    assert called["hit"] is False


# ---------------------------------------------------------------------------
# 9 & 10. Page load creates no content / never pushes or sends
# ---------------------------------------------------------------------------

def test_page_load_creates_no_content_and_no_send(monkeypatch):
    osp = _seed_osp()
    lid = _make_lead_with_buyer_accounts(osp, _USEFUL_BA)

    import src.delivery.instantly as inst
    pushed = {"called": False}

    def _boom(*a, **k):
        pushed["called"] = True
        raise AssertionError("page load must never push/send")

    monkeypatch.setattr(inst, "deliver_email", _boom, raising=False)

    get_lead_full(lid, workspace_id=osp)
    assert pushed["called"] is False
    with session_scope() as s:
        content = s.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == lid)
        ).scalars().all()
        assert content == []


# ---------------------------------------------------------------------------
# 11. Call script + LinkedIn DM generation remain disabled by default
# ---------------------------------------------------------------------------

def test_call_script_and_dm_disabled_by_default():
    from src.icp_config import ICPConfig
    c = ICPConfig()
    assert c.generate_call_script_enabled is False
    assert c.generate_linkedin_dm_enabled is False
