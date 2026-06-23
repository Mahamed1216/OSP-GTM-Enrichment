"""Internal API for SalesOS — endpoint, auth, processing, and safety tests.

No live LLM/HTTP: the pipeline primitives (enrich_lead, enrich_hiring_signal,
score_lead, generate_email) are monkeypatched. TestClient is used WITHOUT a
context manager so the app lifespan (init_db) does not run — the fresh_db
fixture already created the schema and seeds no default workspace.
"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient
from sqlalchemy import select

import src.api.server as apiserver
from src.api.server import app
from src.db import session_scope
from src.models import Enrichment, GeneratedContent, Lead, Score
from src.workspace import create_workspace

API_KEY = "test-internal-key-SECRET-9999"


def _client() -> TestClient:
    return TestClient(app)


def _auth() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


def _ws(slug: str = "osp") -> int:
    return create_workspace(
        name=slug.upper(), slug=slug, instantly_campaign_id=f"camp-{slug}"
    )["id"]


def _sample_request(*, workspace_slug="osp", run_mode="sync", push=False,
                    email_verified=True, ext_id="contact_123",
                    email="john@acme.com") -> dict:
    return {
        "workspace_slug": workspace_slug,
        "source": "salesos",
        "run_mode": run_mode,
        "options": {
            "run_enrichment": True,
            "run_buyer_research": True,
            "run_hiring_signals": True,
            "run_scoring": True,
            "generate_email": True,
            "push_to_instantly": push,
        },
        "leads": [{
            "external_contact_id": ext_id,
            "person": {
                "first_name": "John", "last_name": "Smith", "title": "VP of Sales",
                "email": email, "email_verified": email_verified,
                "linkedin_url": "https://linkedin.com/in/john",
            },
            "company": {
                "name": "Acme", "domain": "acme.com", "website": "https://acme.com",
                "industry": "B2B SaaS", "employee_count": 120,
            },
            "source_signals": [{
                "type": "hiring_leadership", "value": "VP of Quality",
                "confidence": 0.85, "source": "theirstack",
                "source_url": "https://example.com/job",
                "detected_at": "2026-06-05T03:15:00Z",
            }],
            "matched_icps": ["internal-sales"],
            "source_tier": "A", "source_tier_score": 88,
            "raw_payload": {"k": "v"},
        }],
    }


def _install_pipeline_mocks(monkeypatch) -> dict:
    """Patch the 4 heavy primitives so sync processing is fast + deterministic.
    They write real rows so build_processed_payload reflects them."""
    calls = {"enrich": [], "hiring": [], "score": [], "email": []}

    async def fake_enrich(lead_id, *, workspace_id=None):
        calls["enrich"].append(lead_id)
        with session_scope() as s:
            if not s.execute(select(Enrichment).where(Enrichment.lead_id == lead_id)).scalar_one_or_none():
                s.add(Enrichment(
                    lead_id=lead_id, workspace_id=workspace_id,
                    buyer_accounts={
                        "buyer_motion": "B2B",
                        "likely_buyer_segments": ["RevOps teams"],
                        "buyer_research_confidence": "medium",
                        "selected_signal": {"summary": "hiring RevOps",
                                            "source_url": "https://acme.com/job"},
                    },
                ))

    async def fake_hiring(lead_id, *, workspace_id=None, force=False):
        calls["hiring"].append(lead_id)
        return {"skipped": False}

    async def fake_score(lead_id, *, workspace_id=None):
        calls["score"].append(lead_id)
        with session_scope() as s:
            row = s.execute(select(Score).where(Score.lead_id == lead_id)).scalar_one_or_none()
            if row:
                row.score, row.tier = 84, "B"
            else:
                s.add(Score(lead_id=lead_id, score=84, tier="B", rationale="api-test",
                            signals_used=["source signal"], model="test", workspace_id=workspace_id))

        class _R:
            score, tier, rationale, signals_used = 84, "B", "api-test", []
        return _R()

    async def fake_email(lead_id, *, workspace_id=None, **kw):
        calls["email"].append(lead_id)
        with session_scope() as s:
            s.add(GeneratedContent(
                lead_id=lead_id, kind="email", subject="Quick idea",
                body="Hi John — saw Acme is hiring leadership; thought OSP could help.",
                prompt_version="v1", model="test", workspace_id=workspace_id,
            ))
        return None

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead", fake_enrich)
    monkeypatch.setattr("src.signals.hiring.enrich_hiring_signal", fake_hiring)
    monkeypatch.setattr("src.scoring.score_lead", fake_score)
    monkeypatch.setattr("src.content.email.generate_email", fake_email)
    return calls


# ===========================================================================
# 3. /health returns ok
# ===========================================================================

def test_health_ok_public():
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "service": "osp-gtm-enrichment", "version": "v1"}


# ===========================================================================
# 4 & 5. Auth required; invalid key -> 401
# ===========================================================================

def test_process_requires_auth(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    _ws("osp")
    c = _client()
    r = c.post("/api/v1/leads/process", json=_sample_request())
    assert r.status_code == 401


def test_invalid_api_key_returns_401(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    _ws("osp")
    c = _client()
    r = c.post("/api/v1/leads/process", json=_sample_request(),
               headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 401


# ===========================================================================
# 6. Valid API key can submit (async -> queued)
# ===========================================================================

def test_valid_key_can_submit_async(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    _ws("osp")
    c = _client()
    r = c.post("/api/v1/leads/process", json=_sample_request(run_mode="async"),
               headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["lead_count"] == 1
    assert body["run_id"].startswith("run_")


# ===========================================================================
# 7-11. Sync processing: stores raw payload, normalizes signals, source tier
#       separate, email_verified maps, returns full structure
# ===========================================================================

def test_sync_process_full_payload_and_storage(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    ws_id = _ws("osp")
    _install_pipeline_mocks(monkeypatch)
    c = _client()

    r = c.post("/api/v1/leads/process", json=_sample_request(run_mode="sync"),
               headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert len(body["results"]) == 1
    processed = body["results"][0]

    # 11. structure
    for key in ("person", "company", "source_signals", "research", "score",
                "generated_content", "safety", "recommended_action", "metadata"):
        assert key in processed
    assert processed["score"]["tier"] == "B"
    assert processed["research"]["research_confidence"] == "medium"
    assert processed["generated_content"]["content_status"] == "ready"
    assert processed["safety"]["eligible_for_push"] is True

    # 8. Dele source signals normalized (type/value/confidence/source/url/detected_at)
    sig = processed["source_signals"][0]
    assert sig["type"] == "hiring_leadership"
    assert sig["value"] == "VP of Quality"
    assert sig["confidence"] == 0.85
    assert sig["source"] == "theirstack"
    assert sig["source_url"] == "https://example.com/job"
    assert sig["detected_at"] == "2026-06-05T03:15:00Z"

    with session_scope() as s:
        lead = s.execute(
            select(Lead).where(Lead.email == "john@acme.com", Lead.workspace_id == ws_id)
        ).scalar_one()
        # 7. raw source payload stored verbatim
        assert lead.lead_source_raw is not None
        assert lead.lead_source_raw["salesos_payload"]["external_contact_id"] == "contact_123"
        # 9. source tier stored SEPARATELY from local Score tier
        assert lead.source_tier == "A"
        assert lead.source_tier_score == 88.0
        local = s.execute(select(Score).where(Score.lead_id == lead.id)).scalar_one()
        assert local.tier == "B"            # local tier independent of source "A"
        # 10. email_verified=true mapped
        assert lead.email_verification_status == "verified"


# ===========================================================================
# 12 & 13. No auto-push to Instantly, no emails sent
# ===========================================================================

def test_api_does_not_push_or_send(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    ws_id = _ws("osp")
    _install_pipeline_mocks(monkeypatch)

    # Spy any Instantly HTTP — must never be called by the API path.
    import src.delivery.instantly as instmod
    pushed = {"hit": False}

    async def _boom(*a, **k):
        pushed["hit"] = True
        return {"id": "x"}

    monkeypatch.setattr(instmod, "_post_lead_to_campaign", _boom, raising=False)

    c = _client()
    r = c.post("/api/v1/leads/process", json=_sample_request(run_mode="sync"),
               headers=_auth())
    assert r.status_code == 200

    assert pushed["hit"] is False
    with session_scope() as s:
        contents = s.execute(select(GeneratedContent)).scalars().all()
        assert contents  # email content WAS generated
        for ct in contents:
            assert ct.delivered_at is None
            assert (ct.delivery_status != "sent") if ct.delivery_status else True


# ===========================================================================
# 14. push_to_instantly=true is rejected
# ===========================================================================

def test_push_to_instantly_rejected(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    _ws("osp")
    c = _client()
    r = c.post("/api/v1/leads/process", json=_sample_request(push=True), headers=_auth())
    assert r.status_code == 400
    assert r.json()["detail"] == "instant_push_not_supported_via_api"


# ===========================================================================
# 15. Workspace isolation enforced
# ===========================================================================

def test_workspace_isolation(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    ws_osp = _ws("osp")
    ws_other = _ws("other")
    _install_pipeline_mocks(monkeypatch)
    c = _client()

    r = c.post("/api/v1/leads/process",
               json=_sample_request(workspace_slug="osp", run_mode="sync"),
               headers=_auth())
    assert r.status_code == 200
    lead_id = r.json()["results"][0]["lead_id"]

    # The lead belongs to osp only.
    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        assert lead.workspace_id == ws_osp
        other_leads = s.execute(select(Lead).where(Lead.workspace_id == ws_other)).scalars().all()
        assert other_leads == []

    # Reading it scoped to the WRONG workspace must 404.
    r_other = c.get(f"/api/v1/leads/{lead_id}/processed?workspace_slug=other", headers=_auth())
    assert r_other.status_code == 404
    # Reading it scoped to the right workspace works.
    r_osp = c.get(f"/api/v1/leads/{lead_id}/processed?workspace_slug=osp", headers=_auth())
    assert r_osp.status_code == 200
    assert r_osp.json()["lead_id"] == lead_id


# ===========================================================================
# 16. /api/v1/runs/{run_id} returns run status
# ===========================================================================

def test_run_status_endpoint(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    _ws("osp")
    _install_pipeline_mocks(monkeypatch)
    c = _client()

    r = c.post("/api/v1/leads/process", json=_sample_request(run_mode="sync"), headers=_auth())
    run_id = r.json()["run_id"]

    rs = c.get(f"/api/v1/runs/{run_id}", headers=_auth())
    assert rs.status_code == 200
    body = rs.json()
    assert body["run_id"] == run_id
    assert body["status"] == "completed"
    assert body["lead_count"] == 1
    assert body["processed_count"] == 1
    assert body["failed_count"] == 0
    assert body["created_at"] is not None
    assert body["completed_at"] is not None
    assert isinstance(body["results"], list)

    # Run status also requires auth.
    assert c.get(f"/api/v1/runs/{run_id}").status_code == 401

    # Unknown run -> 404.
    assert c.get("/api/v1/runs/run_does_not_exist", headers=_auth()).status_code == 404


# ===========================================================================
# 17. Full API key is never logged
# ===========================================================================

def test_full_api_key_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    _ws("osp")
    c = _client()
    with caplog.at_level(logging.DEBUG, logger="src.api.server"):
        # one valid, one invalid request
        c.post("/api/v1/leads/process", json=_sample_request(run_mode="async"), headers=_auth())
        c.post("/api/v1/leads/process", json=_sample_request(), headers={"Authorization": "Bearer wrong"})
    for rec in caplog.records:
        assert API_KEY not in rec.getMessage()
        assert API_KEY not in str(getattr(rec, "args", ""))


# ===========================================================================
# Bonus: unknown workspace -> 404 (scoping)
# ===========================================================================

def test_unknown_workspace_returns_404(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    c = _client()
    r = c.post("/api/v1/leads/process",
               json=_sample_request(workspace_slug="nope"), headers=_auth())
    assert r.status_code == 404
