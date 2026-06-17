"""Hiring-signal C-tier rescue layer.

Covers the 12 required cases without hitting Tavily or the LLM — the research
call is monkeypatched to a canned result where end-to-end flow is exercised.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from src.context import format_lead_context
from src.db import session_scope
from src.models import GeneratedContent, Lead, LeadSignal, Score
from src.signals.hiring import build_hiring_queries, enrich_hiring_signal
from src.signals.hiring_rescue import run_hiring_rescue, select_c_tier_lead_ids
from src.signals.schemas import HiringSignalResult
from src.signals.store import (
    apply_hiring_uplift,
    get_hiring_signal,
    has_hiring_signal,
    upsert_hiring_signal,
)
from src.signals.uplift import compute_tier_uplift, count_high_weight_roles
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_osp() -> int:
    seed_default_workspace()
    osp_id = get_default_workspace_id()
    assert osp_id is not None
    return osp_id


def _make_lead(email: str, workspace_id: int, *, company="Acme", domain="acme.com") -> int:
    with session_scope() as session:
        lead = Lead(
            first_name="T", last_name="U", email=email,
            company=company, company_domain=domain, workspace_id=workspace_id,
        )
        session.add(lead)
        session.flush()
        return lead.id


def _make_score(lead_id: int, tier: str, score: int, workspace_id: int) -> None:
    with session_scope() as session:
        session.add(Score(
            lead_id=lead_id, score=score, tier=tier,
            rationale="base", signals_used=[], model="test",
            workspace_id=workspace_id,
        ))


def _high_signal(roles: list[str]) -> HiringSignalResult:
    return HiringSignalResult(
        hiring_signal_found=True,
        hiring_signal_strength="high",
        relevant_roles_found=roles,
        relevant_departments=["RevOps", "sales"],
        recency_estimate="unknown",
        why_it_matters="Investing in outbound infra.",
        recommended_email_angle="Saw you're hiring RevOps and an SDR lead.",
        source_urls=["https://acme.com/careers"],
    )


# ---------------------------------------------------------------------------
# 1. Query building uses company name / domain
# ---------------------------------------------------------------------------

def test_build_queries_uses_domain_and_name():
    qs = build_hiring_queries("Acme Corp", "acme.com")
    joined = "\n".join(qs)
    assert any(q.startswith("site:acme.com") for q in qs)
    assert "Acme Corp" in joined
    assert "RevOps" in joined


def test_build_queries_normalizes_website_to_domain():
    qs = build_hiring_queries("Acme", company_website="https://www.acme.com/careers")
    assert any("site:acme.com" in q for q in qs)


def test_build_queries_empty_without_name_or_domain():
    assert build_hiring_queries(None, None) == []


# ---------------------------------------------------------------------------
# 2. High RevOps/GTM hiring signal recommends C_to_B or C_to_A
# ---------------------------------------------------------------------------

def test_high_signal_single_role_recommends_c_to_b():
    rec = compute_tier_uplift(base_tier="C", base_score=55, strength="high", relevant_role_count=1)
    assert rec == "C_to_B"


def test_high_signal_multi_relevant_roles_decent_fit_recommends_c_to_a():
    rec = compute_tier_uplift(base_tier="C", base_score=65, strength="high", relevant_role_count=2)
    assert rec == "C_to_A"


def test_high_signal_multi_roles_but_poor_fit_only_c_to_b():
    # base_score below the DECENT_FIT threshold (60) → no two-tier jump.
    rec = compute_tier_uplift(base_tier="C", base_score=40, strength="high", relevant_role_count=3)
    assert rec == "C_to_B"


def test_high_signal_b_to_a_when_multi_relevant_and_decent():
    rec = compute_tier_uplift(base_tier="B", base_score=72, strength="high", relevant_role_count=2)
    assert rec == "B_to_A"


def test_count_high_weight_roles_matches_canonical_set():
    n = count_high_weight_roles(["VP, Revenue Operations", "SDR Manager", "Office Admin"])
    assert n == 2


# ---------------------------------------------------------------------------
# 3. Medium hiring signal recommends C_to_B only when ICP fit is decent
# ---------------------------------------------------------------------------

def test_medium_signal_c_to_b_when_fit_not_terrible():
    rec = compute_tier_uplift(base_tier="C", base_score=55, strength="medium", relevant_role_count=1)
    assert rec == "C_to_B"


def test_medium_signal_no_bump_when_fit_terrible():
    rec = compute_tier_uplift(base_tier="C", base_score=30, strength="medium", relevant_role_count=2)
    assert rec == "none"


# ---------------------------------------------------------------------------
# 4. Low / no hiring signal does not bump
# ---------------------------------------------------------------------------

def test_low_signal_no_bump():
    assert compute_tier_uplift(base_tier="C", base_score=90, strength="low", relevant_role_count=5) == "none"


def test_no_signal_no_bump():
    assert compute_tier_uplift(base_tier="C", base_score=90, strength="none", relevant_role_count=5) == "none"


def test_tier_d_never_rescued():
    assert compute_tier_uplift(base_tier="D", base_score=90, strength="high", relevant_role_count=5) == "none"


# ---------------------------------------------------------------------------
# 5. Hiring signal is stored with workspace_id
# ---------------------------------------------------------------------------

def test_signal_stored_with_workspace_id():
    osp = _seed_osp()
    lead_id = _make_lead("a@x.com", osp)
    upsert_hiring_signal(lead_id, _high_signal(["RevOps"]), workspace_id=osp)
    row = get_hiring_signal(lead_id)
    assert row is not None
    assert row["workspace_id"] == osp
    assert row["signal_strength"] == "high"
    assert row["relevant_roles"] == ["RevOps"]


# ---------------------------------------------------------------------------
# 6. Workspace A hiring signal does not appear in Workspace B
# ---------------------------------------------------------------------------

def test_signal_isolation_between_workspaces():
    osp = _seed_osp()
    other = create_workspace(name="WS B", slug="ws-b", instantly_campaign_id="c-b")["id"]

    lead_a = _make_lead("a@x.com", osp)
    _make_score(lead_a, "C", 55, osp)
    upsert_hiring_signal(lead_a, _high_signal(["RevOps"]), workspace_id=osp)

    lead_b = _make_lead("b@x.com", other)
    _make_score(lead_b, "C", 55, other)

    # Workspace B's rescue selection never returns workspace A's lead.
    ids_b = select_c_tier_lead_ids(other, force=True)
    assert lead_a not in ids_b
    assert lead_b in ids_b


# ---------------------------------------------------------------------------
# Uplift application end-to-end on the Score row
# ---------------------------------------------------------------------------

def test_apply_uplift_bumps_score_row_and_is_idempotent():
    osp = _seed_osp()
    lead_id = _make_lead("c@x.com", osp)
    _make_score(lead_id, "C", 65, osp)
    upsert_hiring_signal(lead_id, _high_signal(["RevOps", "SDR Manager"]), workspace_id=osp)

    out = apply_hiring_uplift(lead_id, workspace_id=osp)
    assert out["applied"] is True
    assert out["new_tier"] == "A"

    with session_scope() as session:
        score = session.execute(select(Score).where(Score.lead_id == lead_id)).scalar_one()
        assert score.tier == "A"
        assert score.score >= 85
        assert any(str(s).startswith("hiring:") for s in score.signals_used)

    # Re-applying must not compound (still A, single marker).
    apply_hiring_uplift(lead_id, workspace_id=osp)
    with session_scope() as session:
        score = session.execute(select(Score).where(Score.lead_id == lead_id)).scalar_one()
        assert score.tier == "A"
        markers = [s for s in score.signals_used if str(s).startswith("hiring:")]
        assert len(markers) == 1


# ---------------------------------------------------------------------------
# 7. Non-C tier leads are not processed by the default rescue command
# ---------------------------------------------------------------------------

def test_rescue_selection_only_c_tier():
    osp = _seed_osp()
    c_lead = _make_lead("c@x.com", osp)
    _make_score(c_lead, "C", 55, osp)
    b_lead = _make_lead("b@x.com", osp)
    _make_score(b_lead, "B", 75, osp)

    ids = select_c_tier_lead_ids(osp)
    assert c_lead in ids
    assert b_lead not in ids


# ---------------------------------------------------------------------------
# 8. Leads with an existing hiring signal are skipped unless rerun is requested
# ---------------------------------------------------------------------------

def test_rescue_selection_skips_existing_unless_forced():
    osp = _seed_osp()
    lead_id = _make_lead("c@x.com", osp)
    _make_score(lead_id, "C", 55, osp)
    upsert_hiring_signal(lead_id, _high_signal(["RevOps"]), workspace_id=osp)

    assert lead_id not in select_c_tier_lead_ids(osp)             # default: skip
    assert lead_id in select_c_tier_lead_ids(osp, force=True)     # force: include


def test_enrich_skips_existing_signal_without_force(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("c@x.com", osp)
    _make_score(lead_id, "C", 55, osp)
    upsert_hiring_signal(lead_id, _high_signal(["RevOps"]), workspace_id=osp)

    # research must NOT run when a signal already exists and force=False.
    called = {"research": False}

    async def _boom(*a, **k):
        called["research"] = True
        return _high_signal(["RevOps"])

    monkeypatch.setattr("src.signals.hiring.research_hiring_signal", _boom)
    out = asyncio.run(enrich_hiring_signal(lead_id, workspace_id=osp, force=False))
    assert out["skipped"] is True
    assert called["research"] is False


# ---------------------------------------------------------------------------
# 9. New imported C-tier leads run hiring enrichment before scoring/content
# ---------------------------------------------------------------------------

def test_process_imported_leads_order_hiring_before_scoring(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("new@x.com", osp)

    order: list[str] = []

    import src.lead_source.scheduler as sched

    monkeypatch.setattr(sched, "_has_enrichment", lambda lid: True)  # skip enrichment

    async def fake_hiring(lid, *, workspace_id=None, force=False):
        order.append("hiring")
        return {"lead_id": lid, "skipped": False, "found": True, "strength": "high"}

    async def fake_score(lid, ws):
        order.append("score")
        return True

    async def fake_content(lid, ws):
        order.append("content")
        return 1

    monkeypatch.setattr("src.signals.hiring.enrich_hiring_signal", fake_hiring)
    monkeypatch.setattr(sched, "_score_one", fake_score)
    monkeypatch.setattr(sched, "_generate_content_one", fake_content)

    asyncio.run(sched.process_imported_leads([lead_id], osp))
    assert order == ["hiring", "score", "content"]


# ---------------------------------------------------------------------------
# 10. Generated content can access the hiring-signal angle
# ---------------------------------------------------------------------------

def test_context_includes_hiring_angle():
    lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Acme", industry="SaaS")
    hiring = {
        "signal_found": True,
        "signal_strength": "high",
        "relevant_roles": ["RevOps", "SDR Manager"],
        "relevant_departments": ["RevOps"],
        "why_it_matters": "Investing in outbound.",
        "recommended_email_angle": "Saw you're hiring RevOps and an SDR lead.",
    }
    out = format_lead_context(lead, None, None, hiring_signal=hiring)
    assert "Hiring signal" in out
    assert "RevOps" in out
    assert "SDR Manager" in out
    assert "Saw you're hiring RevOps" in out


def test_context_omits_hiring_block_when_not_found():
    lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Acme")
    out = format_lead_context(lead, None, None, hiring_signal={"signal_found": False})
    assert "Hiring signal" not in out


# ---------------------------------------------------------------------------
# 11 & 12. The rescue run never sends email or pushes to Instantly
# ---------------------------------------------------------------------------

def test_rescue_run_never_sends_or_pushes(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("c@x.com", osp)
    _make_score(lead_id, "C", 65, osp)

    pushed = {"deliver": False}

    async def fake_research(*a, **k):
        return _high_signal(["RevOps", "SDR Manager"])

    def fake_deliver(*a, **k):
        pushed["deliver"] = True
        raise AssertionError("delivery must never be called by the rescue")

    monkeypatch.setattr("src.signals.hiring.research_hiring_signal", fake_research)
    monkeypatch.setattr("src.delivery.instantly.deliver_email", fake_deliver)

    report = asyncio.run(run_hiring_rescue(osp, limit=10))

    assert pushed["deliver"] is False
    assert report.processed_count == 1
    assert report.high_signal_count == 1
    # high signal + 2 relevant roles + decent fit (65) → C_to_A bump.
    assert report.bumped_to_A_count == 1

    # No email content rows were created by the rescue.
    with session_scope() as session:
        emails = session.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == lead_id)
        ).scalars().all()
        assert emails == []

    # The signal was persisted and the Score row uplifted.
    assert has_hiring_signal(lead_id)
    with session_scope() as session:
        score = session.execute(select(Score).where(Score.lead_id == lead_id)).scalar_one()
        assert score.tier == "A"
