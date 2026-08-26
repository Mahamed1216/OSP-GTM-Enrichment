"""Imported source signals / enrichment from the OSP Lead Engine payload.

Covers the 12 required cases plus the pure parser. No live HTTP/LLM — imports
use import_contacts directly with hand-built ContactOut dicts.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from src.context import format_lead_context
from src.db import session_scope
from src.lead_source.ingest import SKIP_DUPLICATE, import_contacts, start_import_log
from src.lead_source.source_signals import parse_source_signals
from src.models import GeneratedContent, Lead, LeadSignal, Score
from src.signals.store import apply_signal_uplifts, get_source_signal, upsert_source_signal
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_osp() -> int:
    seed_default_workspace()
    osp_id = get_default_workspace_id()
    assert osp_id is not None
    return osp_id


def _contact(
    *,
    id="ext-1",
    email="alice@acme.com",
    company="Acme Corp",
    domain="acme.com",
    signals=None,
    matched_icps=None,
    tier=None,
    tier_score=None,
    source="osp_lead_engine",
    enrichment_status="enriched",
    email_verified=True,
) -> dict:
    c = {
        "id": id,
        "email": email,
        "first_name": "Alice",
        "last_name": "Smith",
        "company_name": company,
        "company_domain": domain,
        "title": "VP Sales",
        "source": source,
        "enrichment_status": enrichment_status,
        "email_verified": email_verified,
        "created_at": "2026-01-01T00:00:00",
    }
    if signals is not None:
        c["signals"] = signals
    if matched_icps is not None:
        c["matched_icps"] = matched_icps
    if tier is not None:
        c["tier"] = tier
    if tier_score is not None:
        c["tier_score"] = tier_score
    return c


def _import(contacts, ws_id, slug="osp"):
    log_id = start_import_log(ws_id, slug, requested_limit=25)
    return import_contacts(contacts, workspace_id=ws_id, client_slug=slug, import_id=log_id)


_TWO_STRONG = [
    {"name": "Hiring RevOps", "strength": "high", "summary": "3 open RevOps roles",
     "url": "https://acme.com/careers", "detected_at": "2026-01-02"},
    {"name": "Series B funding", "strength": "high", "summary": "Raised $40M"},
]


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------

def test_parse_structured_signals():
    parsed = parse_source_signals(_contact(signals=_TWO_STRONG, matched_icps=["saas-vp"], tier="A", tier_score=95))
    assert parsed.has_signals is True
    assert parsed.overall_strength == "high"
    assert parsed.strong_signal_count == 2
    assert parsed.source_tier == "A"
    assert parsed.source_tier_score == 95.0
    assert parsed.matched_icps == ["saas-vp"]
    assert len(parsed.signals) == 2
    assert parsed.signals[0]["signal_name"] == "Hiring RevOps"
    assert parsed.signals[0]["signal_source_url"] == "https://acme.com/careers"


def test_parse_unstructured_signal_is_low():
    parsed = parse_source_signals(_contact(signals=["company is hiring"]))
    assert parsed.has_signals is True
    assert parsed.overall_strength == "low"
    assert parsed.strong_signal_count == 0


def test_parse_no_signals():
    parsed = parse_source_signals(_contact(signals=[]))
    assert parsed.has_signals is False
    assert parsed.summary == ""


def test_parse_numeric_score_strength():
    parsed = parse_source_signals(_contact(signals=[{"name": "intent", "score": 0.85}]))
    assert parsed.overall_strength == "high"


# ---------------------------------------------------------------------------
# 1. Import stores full raw payload
# ---------------------------------------------------------------------------

def test_import_stores_full_raw_payload():
    osp = _seed_osp()
    contact = _contact(signals=_TWO_STRONG, tier="A", tier_score=90)
    _import([contact], osp)
    with session_scope() as s:
        lead = s.execute(select(Lead).where(Lead.email == "alice@acme.com")).scalar_one()
        assert lead.lead_source_raw is not None
        assert lead.lead_source_raw["signals"] == _TWO_STRONG
        assert lead.lead_source_raw["tier"] == "A"


# ---------------------------------------------------------------------------
# 2. Import stores payload.signals when present (normalized lead_signals row)
# ---------------------------------------------------------------------------

def test_import_stores_signals_row():
    osp = _seed_osp()
    _import([_contact(signals=_TWO_STRONG, matched_icps=["saas-vp"])], osp)
    with session_scope() as s:
        lead_id = s.execute(select(Lead.id).where(Lead.email == "alice@acme.com")).scalar_one()
    row = get_source_signal(lead_id)
    assert row is not None
    assert row["signal_found"] is True
    assert row["signal_strength"] == "high"
    assert row["relevant_departments"] == ["saas-vp"]


# ---------------------------------------------------------------------------
# 3 & 4. Lead detail data path: section data present / absent
# ---------------------------------------------------------------------------

def test_get_lead_full_exposes_source_signal():
    osp = _seed_osp()
    _import([_contact(signals=_TWO_STRONG, tier="A", tier_score=88)], osp)
    with session_scope() as s:
        lead_id = s.execute(select(Lead.id).where(Lead.email == "alice@acme.com")).scalar_one()

    from src.lib.db_queries import get_lead_full
    bundle = get_lead_full(lead_id, workspace_id=osp)
    assert bundle["lead"]["lead_source_raw"] is not None      # raw payload expandable
    assert bundle["lead"]["source_tier"] == "A"
    assert bundle["source_signal"] is not None
    assert bundle["source_signal"]["signal_found"] is True


def test_get_lead_full_no_source_signal_when_payload_empty():
    osp = _seed_osp()
    _import([_contact(signals=[])], osp)
    with session_scope() as s:
        lead_id = s.execute(select(Lead.id).where(Lead.email == "alice@acme.com")).scalar_one()

    from src.lib.db_queries import get_lead_full
    bundle = get_lead_full(lead_id, workspace_id=osp)
    # UI renders "No source signals were included in the imported payload."
    assert bundle["source_signal"] is None


# ---------------------------------------------------------------------------
# 5. Imported signals are workspace scoped
# ---------------------------------------------------------------------------

def test_source_signals_workspace_scoped():
    osp = _seed_osp()
    other = create_workspace(name="WS B", slug="ws-b", instantly_campaign_id="c-b")["id"]

    _import([_contact(id="a", email="a@acme.com", signals=_TWO_STRONG)], osp)
    _import([_contact(id="b", email="b@acme.com", signals=[])], other)

    with session_scope() as s:
        osp_rows = s.execute(
            select(LeadSignal).where(
                LeadSignal.workspace_id == osp,
                LeadSignal.signal_type == "source_import",
            )
        ).scalars().all()
        other_rows = s.execute(
            select(LeadSignal).where(
                LeadSignal.workspace_id == other,
                LeadSignal.signal_type == "source_import",
            )
        ).scalars().all()
    assert len(osp_rows) == 1
    assert len(other_rows) == 0


# ---------------------------------------------------------------------------
# 6. Source tier/tier_score stored separately from local tier
# ---------------------------------------------------------------------------

def test_source_tier_stored_separately_from_local_tier():
    osp = _seed_osp()
    _import([_contact(signals=_TWO_STRONG, tier="A", tier_score=92)], osp)
    with session_scope() as s:
        lead = s.execute(select(Lead).where(Lead.email == "alice@acme.com")).scalar_one()
        # Source tier captured on the Lead, separate from any local Score.
        assert lead.source_tier == "A"
        assert lead.source_tier_score == 92.0
        # No local Score was created or set to "A" by the import.
        local = s.execute(select(Score).where(Score.lead_id == lead.id)).scalar_one_or_none()
        assert local is None


# ---------------------------------------------------------------------------
# 7. Scoring can use a high-confidence imported signal as uplift evidence
# ---------------------------------------------------------------------------

def test_imported_signal_drives_tier_uplift():
    osp = _seed_osp()
    _import([_contact(signals=_TWO_STRONG, matched_icps=["saas-vp"])], osp)
    with session_scope() as s:
        lead = s.execute(select(Lead).where(Lead.email == "alice@acme.com")).scalar_one()
        lead_id = lead.id
        s.add(Score(lead_id=lead_id, score=65, tier="C", rationale="base",
                    signals_used=[], model="test", workspace_id=osp))

    out = apply_signal_uplifts(lead_id, workspace_id=osp, base_tier="C", base_score=65)
    assert out["applied"] is True
    # 2 strong source signals + decent fit (65) → C_to_A.
    assert out["new_tier"] == "A"
    with session_scope() as s:
        score = s.execute(select(Score).where(Score.lead_id == lead_id)).scalar_one()
        assert score.tier == "A"
        assert any(str(x).startswith("source:") for x in score.signals_used)


def test_weak_imported_signal_does_not_bump():
    osp = _seed_osp()
    _import([_contact(signals=["company exists"])], osp)  # unstructured → low
    with session_scope() as s:
        lead_id = s.execute(select(Lead.id).where(Lead.email == "alice@acme.com")).scalar_one()
        s.add(Score(lead_id=lead_id, score=65, tier="C", rationale="base",
                    signals_used=[], model="test", workspace_id=osp))

    out = apply_signal_uplifts(lead_id, workspace_id=osp, base_tier="C", base_score=65)
    assert out["applied"] is False
    with session_scope() as s:
        score = s.execute(select(Score).where(Score.lead_id == lead_id)).scalar_one()
        assert score.tier == "C"


# ---------------------------------------------------------------------------
# 8. Content generation can access the imported signal angle
# ---------------------------------------------------------------------------

def test_context_includes_source_signal_angle():
    lead = Lead(first_name="A", last_name="B", email="a@x.com", company="Acme")
    source = {
        "signal_found": True,
        "signal_strength": "high",
        "relevant_roles": ["Hiring RevOps", "Series B funding"],
        "relevant_departments": ["saas-vp"],
        "summary": "3 open RevOps roles; raised $40M",
        "recommended_email_angle": "Saw you're hiring RevOps after the Series B.",
    }
    out = format_lead_context(lead, None, None, source_signal=source)
    assert "Source signal (imported from lead engine)" in out
    assert "Hiring RevOps" in out
    assert "Saw you're hiring RevOps" in out


# ---------------------------------------------------------------------------
# 9. Import log counts leads with and without source signals
# ---------------------------------------------------------------------------

def test_import_log_counts_with_without_source_signals():
    osp = _seed_osp()
    contacts = [
        _contact(id="a", email="a@acme.com", signals=_TWO_STRONG),
        _contact(id="b", email="b@acme.com", signals=[]),
        _contact(id="c", email="c@acme.com", signals=[{"name": "intent", "strength": "medium"}]),
    ]
    result = _import(contacts, osp)
    assert result.with_source_signals == 2
    assert result.without_source_signals == 1
    summary = result.to_summary()
    assert summary["with_source_signals"] == 2
    assert summary["without_source_signals"] == 1


# ---------------------------------------------------------------------------
# 10. Existing dedupe behavior still works
# ---------------------------------------------------------------------------

def test_dedupe_still_works_on_reimport():
    osp = _seed_osp()
    contact = _contact(signals=_TWO_STRONG)
    r1 = _import([contact], osp)
    assert r1.created == 1
    r2 = _import([contact], osp)  # same external id + email
    assert r2.created == 0
    assert r2.skip_reasons.get(SKIP_DUPLICATE, 0) == 1
    with session_scope() as s:
        n = s.execute(select(Lead).where(Lead.email == "alice@acme.com")).scalars().all()
        assert len(n) == 1  # no duplicate row


# ---------------------------------------------------------------------------
# 11 & 12. Import never pushes to Instantly and never sends email
# ---------------------------------------------------------------------------

def test_import_does_not_push_or_send(monkeypatch):
    osp = _seed_osp()

    import src.delivery.instantly as instantly_mod
    pushed = {"called": False}

    def _boom(*a, **k):
        pushed["called"] = True
        raise AssertionError("import must never push/send")

    monkeypatch.setattr(instantly_mod, "deliver_email", _boom, raising=False)

    _import([_contact(signals=_TWO_STRONG)], osp)

    assert pushed["called"] is False
    with session_scope() as s:
        # No content generated, nothing delivered.
        content = s.execute(select(GeneratedContent)).scalars().all()
        assert content == []
