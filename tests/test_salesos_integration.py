"""SalesOS shared-Supabase integration tests.

Covers the adapter mapping, the processing worker (claim/process/results/error),
the approval-gated send path (block without approval; allow with approval +
safety; unsafe/unverified/duplicate still block even when approved; missing
approval never marks sent), and that the standalone flow + Streamlit + Docker
docs are preserved.

No live LLM/HTTP: the pipeline primitives and Instantly delivery are
monkeypatched. The contract tables are created by the conftest fresh_db fixture
because this module imports the SalesOS models at collection time.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.processing import salesos_lead_to_contact
from src.config import settings
from src.db import session_scope
from src.delivery.eligibility import filter_eligible
from src.delivery.instantly import DeliveryResult, deliver_email
from src.integrations.salesos.adapter import (
    claim_and_process,
    claim_job,
    salesos_lead_to_nested,
)
from src.integrations.salesos.models import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    CONTENT_PENDING_REVIEW,
    CONTENT_SENT,
    DeliveryEvent,
    LeadEnrichment,
    LeadScore,
    OutboundApproval,
    OutboundContent,
    OutboundJob,
    SalesOSLead,
)
from src.integrations.salesos.sending import send_approved_once
from src.integrations.salesos.worker import drain_once
from src.models import GeneratedContent, Lead, Score, now_utc
from src.workspace import create_workspace

_ROOT = pathlib.Path(__file__).resolve().parents[1]
SAFE_BODY = "Hi John — saw Acme is scaling RevOps; thought OSP could help. Best, M"
UNSAFE_BODY = "NEEDS REVIEW: buyer research incomplete, do not send"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def integration_mode(monkeypatch):
    monkeypatch.setattr(settings, "salesos_integration_mode", True)
    yield


def _ws(slug: str = "osp") -> int:
    return create_workspace(
        name=slug.upper(), slug=slug, instantly_campaign_id=f"camp-{slug}"
    )["id"]


def _salesos_lead(ws_id: int, **over) -> str:
    defaults = dict(
        workspace_id=ws_id,
        client_id="acme",
        external_contact_id="contact_123",
        source="salesos",
        first_name="John",
        last_name="Smith",
        title="VP of Sales",
        email="john@acme.com",
        email_verified=True,
        linkedin_url="https://linkedin.com/in/john",
        company_name="Acme",
        company_domain="acme.com",
        company_website="https://acme.com",
        company_industry="B2B SaaS",
        raw_source_payload={"k": "v"},
        source_signals=[{
            "type": "hiring_leadership", "value": "VP of Quality",
            "confidence": 0.85, "source": "theirstack",
            "source_url": "https://example.com/job",
            "detected_at": "2026-06-05T03:15:00Z",
        }],
        source_tier="A",
        source_tier_score=88.0,
    )
    defaults.update(over)
    with session_scope() as s:
        row = SalesOSLead(**defaults)
        s.add(row)
        s.flush()
        return row.id


def _job(salesos_lead_id: str, ws_id: int, options: dict | None = None,
         client_id: str = "acme") -> str:
    with session_scope() as s:
        job = OutboundJob(
            lead_id=salesos_lead_id, workspace_id=ws_id, client_id=client_id,
            status="queued", requested_by="csm@acme.com", options=options,
        )
        s.add(job)
        s.flush()
        return job.id


def _install_pipeline_mocks(monkeypatch, *, fail_step: str | None = None) -> dict:
    """Patch the heavy primitives so processing is fast + deterministic.
    They write real engine rows so mirror_results reflects them."""
    calls = {"enrich": [], "hiring": [], "score": [], "email": []}

    async def fake_enrich(lead_id, *, workspace_id=None):
        if fail_step == "enrich":
            raise RuntimeError("boom-enrich")
        calls["enrich"].append(lead_id)
        with session_scope() as s:
            from src.models import Enrichment
            if not s.execute(select(Enrichment).where(Enrichment.lead_id == lead_id)).scalar_one_or_none():
                s.add(Enrichment(
                    lead_id=lead_id, workspace_id=workspace_id,
                    buyer_accounts={
                        "buyer_motion": "B2B",
                        "likely_buyer_segments": ["RevOps teams"],
                        "buyer_research_confidence": "medium",
                    },
                ))

    async def fake_hiring(lead_id, *, workspace_id=None, force=False):
        calls["hiring"].append(lead_id)
        return {"skipped": False}

    async def fake_score(lead_id, *, workspace_id=None):
        if fail_step == "score":
            raise RuntimeError("boom-score")
        calls["score"].append(lead_id)
        with session_scope() as s:
            row = s.execute(select(Score).where(Score.lead_id == lead_id)).scalar_one_or_none()
            if row:
                row.score, row.tier = 80, "B"
            else:
                s.add(Score(lead_id=lead_id, score=80, tier="B", rationale="sos-test",
                            signals_used=["source signal"], model="test", workspace_id=workspace_id))
        return None

    async def fake_email(lead_id, *, workspace_id=None, **kw):
        calls["email"].append(lead_id)
        with session_scope() as s:
            s.add(GeneratedContent(
                lead_id=lead_id, kind="email", subject="Quick idea",
                body=SAFE_BODY, prompt_version="v1", model="test", workspace_id=workspace_id,
            ))
        return None

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead", fake_enrich)
    monkeypatch.setattr("src.signals.hiring.enrich_hiring_signal", fake_hiring)
    monkeypatch.setattr("src.scoring.score_lead", fake_score)
    monkeypatch.setattr("src.content.email.generate_email", fake_email)
    return calls


def _engine_lead(ws_id: int, *, verified=True, tier="B", body=SAFE_BODY,
                 delivered=False) -> tuple[int, int]:
    """Create an engine Lead + Score + email content. Returns (lead_id, content_id)."""
    with session_scope() as s:
        lead = Lead(
            first_name="John", last_name="Smith", email="john@acme.com",
            title="VP of Sales", company="Acme", company_domain="acme.com",
            workspace_id=ws_id, source_tier="A", source_tier_score=88.0,
        )
        if verified:
            lead.email_verification_status = "verified"
            lead.email_verification_provider = "osp_lead_engine"
            lead.email_verified_at = now_utc()
        else:
            lead.email_verification_status = "invalid"
        s.add(lead)
        s.flush()
        lead_id = lead.id
        s.add(Score(lead_id=lead_id, score=80, tier=tier, rationale="t",
                    signals_used=[], model="test", workspace_id=ws_id))
        content = GeneratedContent(
            lead_id=lead_id, kind="email", subject="Quick idea", body=body,
            prompt_version="v1", model="test", workspace_id=ws_id,
        )
        if delivered:
            content.delivery_status = "sent"
            content.delivery_id = "remote-existing"
            content.delivered_at = now_utc()
        s.add(content)
        s.flush()
        return lead_id, content.id


def _approved_chain(ws_id: int, *, verified=True, body=SAFE_BODY,
                    approval_status=APPROVAL_APPROVED, delivered=False) -> dict:
    """Build the full SalesOS approved-content chain over an engine lead."""
    engine_lead_id, engine_content_id = _engine_lead(
        ws_id, verified=verified, body=body, delivered=delivered
    )
    salesos_lead_id = _salesos_lead(ws_id)
    with session_scope() as s:
        oc = OutboundContent(
            lead_id=salesos_lead_id, email_subject="Quick idea", email_body=body,
            content_status=CONTENT_PENDING_REVIEW, engine_content_id=engine_content_id,
        )
        s.add(oc)
        s.flush()
        oc_id = oc.id
        appr = OutboundApproval(
            lead_id=salesos_lead_id, content_id=oc_id,
            approval_status=approval_status, approved_by="csm@acme.com",
            approved_at=now_utc(),
        )
        s.add(appr)
    return {
        "engine_lead_id": engine_lead_id, "engine_content_id": engine_content_id,
        "salesos_lead_id": salesos_lead_id, "content_id": oc_id,
    }


# ===========================================================================
# 1. Adapter maps a SalesOS lead to the internal lead shape
# ===========================================================================

def test_adapter_maps_salesos_lead_to_internal_shape():
    row = SalesOSLead(
        workspace_id=1, external_contact_id="contact_123", source="salesos",
        first_name="John", last_name="Smith", title="VP of Sales",
        email="john@acme.com", email_verified=True,
        linkedin_url="https://linkedin.com/in/john", company_name="Acme",
        company_domain="acme.com", company_industry="B2B SaaS",
        source_signals=[{"type": "hiring_leadership", "value": "VP of Quality"}],
        source_tier="A", source_tier_score=88.0,
        raw_source_payload={"k": "v"},
    )
    nested = salesos_lead_to_nested(row)
    contact = salesos_lead_to_contact(nested, source="salesos")

    assert contact["first_name"] == "John"
    assert contact["last_name"] == "Smith"
    assert contact["email"] == "john@acme.com"
    assert contact["company_name"] == "Acme"
    assert contact["company_domain"] == "acme.com"
    assert contact["linkedin_url"] == "https://linkedin.com/in/john"
    assert contact["email_verified"] is True
    # 2. Source signals preserved into the shape the parser understands.
    assert contact["signals"] == [{"type": "hiring_leadership", "value": "VP of Quality"}]
    # Source tier flows through as `tier`/`tier_score` for the source-signal parser.
    assert contact["tier"] == "A"
    assert contact["tier_score"] == 88.0
    # Verbatim original preserved for provenance.
    assert contact["salesos_payload"] == nested


# ===========================================================================
# 4. Worker safely claims a queued job
# ===========================================================================

def test_worker_claims_queued_job():
    ws = _ws("osp")
    sl = _salesos_lead(ws)
    job_id = _job(sl, ws)

    assert claim_job(job_id) is True
    with session_scope() as s:
        assert s.get(OutboundJob, job_id).status == "running"
    # Second claim must fail — only one worker can win.
    assert claim_job(job_id) is False


def test_worker_dry_run_claims_nothing():
    ws = _ws("osp")
    sl = _salesos_lead(ws)
    _job(sl, ws)
    summary = drain_once(limit=10, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["queued"] == 1
    assert summary["processed"] == 0
    with session_scope() as s:
        assert s.execute(select(OutboundJob)).scalar_one().status == "queued"


# ===========================================================================
# 5 + 2 + 3. Worker writes enrichment/score/content; signals + source tier kept
# ===========================================================================

def test_worker_processes_and_writes_results(monkeypatch):
    ws = _ws("osp")
    _install_pipeline_mocks(monkeypatch)
    sl = _salesos_lead(ws)
    job_id = _job(sl, ws)

    summary = drain_once(limit=10)
    assert summary["processed"] == 1

    with session_scope() as s:
        job = s.get(OutboundJob, job_id)
        assert job.status == "completed"
        assert job.engine_lead_id is not None
        engine_lead_id = job.engine_lead_id

        # Engine lead created from the SalesOS lead (internal shape mapping).
        lead = s.get(Lead, engine_lead_id)
        assert lead.email == "john@acme.com"
        assert lead.company == "Acme"
        assert lead.workspace_id == ws
        # 2. Source signals preserved on the imported lead.
        assert lead.lead_source_raw is not None
        assert lead.lead_source_raw.get("signals")
        # 3. Source tier stored SEPARATELY from local tier.
        assert lead.source_tier == "A"
        local_score = s.execute(select(Score).where(Score.lead_id == engine_lead_id)).scalar_one()
        assert local_score.tier == "B"

        # 5. Results mirrored into SalesOS contract tables.
        enr = s.execute(select(LeadEnrichment).where(LeadEnrichment.lead_id == sl)).scalar_one()
        assert enr.buyer_account_research["buyer_motion"] == "B2B"
        sos_score = s.execute(select(LeadScore).where(LeadScore.lead_id == sl)).scalar_one()
        assert sos_score.tier == "B"                  # engine tier, NOT source "A"
        sos_lead = s.get(SalesOSLead, sl)
        assert sos_lead.source_tier == "A"            # source tier untouched
        content = s.execute(select(OutboundContent).where(OutboundContent.lead_id == sl)).scalar_one()
        assert content.content_status == CONTENT_PENDING_REVIEW
        assert content.email_body == SAFE_BODY
        assert content.engine_content_id is not None
        assert content.safety_status == "ok"


# ===========================================================================
# 6. A failed worker job writes the error
# ===========================================================================

def test_failed_worker_job_writes_error(monkeypatch):
    ws = _ws("osp")
    _install_pipeline_mocks(monkeypatch, fail_step="score")
    sl = _salesos_lead(ws)
    job_id = _job(sl, ws)

    summary = drain_once(limit=10)
    assert summary["processed"] == 0
    with session_scope() as s:
        job = s.get(OutboundJob, job_id)
        assert job.status == "failed"
        assert job.error and "boom-score" in job.error
        assert job.completed_at is not None


# ===========================================================================
# 7. Standalone flow still works when SALESOS_INTEGRATION_MODE=false
# ===========================================================================

def test_standalone_flow_unaffected_when_mode_off(monkeypatch):
    # Default flag is False; assert no approval is required for a clean lead.
    monkeypatch.setattr(settings, "salesos_integration_mode", False)
    ws = _ws("osp")
    lead_id, _cid = _engine_lead(ws)
    with session_scope() as s:
        eligible, skipped = filter_eligible([lead_id], s)
    assert eligible == [lead_id]
    assert skipped["missing_salesos_csm_approval"] == []


# ===========================================================================
# 8 + 10. Send blocked without CSM approval; never marked sent (mode on)
# ===========================================================================

def test_send_blocked_without_approval(integration_mode):
    ws = _ws("osp")
    lead_id, content_id = _engine_lead(ws)
    sl = _salesos_lead(ws)
    # Content exists + linked but NO approval row.
    with session_scope() as s:
        s.add(OutboundContent(lead_id=sl, email_body=SAFE_BODY,
                              content_status=CONTENT_PENDING_REVIEW,
                              engine_content_id=content_id))

    # Shared eligibility gate blocks with the dedicated reason.
    with session_scope() as s:
        eligible, skipped = filter_eligible([lead_id], s)
    assert lead_id not in eligible
    assert lead_id in skipped["missing_salesos_csm_approval"]


def test_missing_approval_does_not_mark_sent(integration_mode):
    ws = _ws("osp")
    lead_id, content_id = _engine_lead(ws)
    # deliver_email directly (no approval) must skip and never mark sent.
    import asyncio
    result = asyncio.run(deliver_email(lead_id, workspace_id=ws))
    assert result.delivered is False
    assert result.skip_reason == "missing_salesos_csm_approval"
    with session_scope() as s:
        content = s.get(GeneratedContent, content_id)
        assert content.delivery_status != "sent"
        assert content.delivered_at is None


def test_pending_approval_not_picked_up_by_send_worker(integration_mode):
    ws = _ws("osp")
    _approved_chain(ws, approval_status=APPROVAL_PENDING)
    summary = send_approved_once(limit=10)
    assert summary["found"] == 0
    assert summary["sent"] == 0


# ===========================================================================
# 9. Send allowed with approval + all safety checks (mode on)
# ===========================================================================

def test_send_allowed_with_approval(integration_mode, monkeypatch):
    ws = _ws("osp")
    chain = _approved_chain(ws)

    # Approved + all safety passes → the shared gate marks it eligible.
    with session_scope() as s:
        eligible, _ = filter_eligible([chain["engine_lead_id"]], s)
    assert chain["engine_lead_id"] in eligible

    sent_calls = {"n": 0}

    async def fake_deliver(lead_id, *, dry_run=False, strict_verification=False, workspace_id=None):
        sent_calls["n"] += 1
        return DeliveryResult(delivered=True, delivery_id="remote-OK")

    monkeypatch.setattr("src.delivery.instantly.deliver_email", fake_deliver)

    summary = send_approved_once(limit=10)
    assert summary["found"] == 1
    assert summary["sent"] == 1
    assert summary["blocked"] == 0
    assert sent_calls["n"] == 1

    with session_scope() as s:
        ev = s.execute(select(DeliveryEvent)).scalar_one()
        assert ev.status == "sent"
        assert ev.instantly_lead_id == "remote-OK"
        oc = s.get(OutboundContent, chain["content_id"])
        assert oc.content_status == CONTENT_SENT


# ===========================================================================
# 11. Unsafe content blocks even when approved
# ===========================================================================

def test_unsafe_content_blocks_even_when_approved(integration_mode, monkeypatch):
    ws = _ws("osp")
    _approved_chain(ws, body=UNSAFE_BODY)

    boom = {"hit": False}

    async def fake_deliver(*a, **k):
        boom["hit"] = True
        return DeliveryResult(delivered=True, delivery_id="x")

    monkeypatch.setattr("src.delivery.instantly.deliver_email", fake_deliver)

    summary = send_approved_once(limit=10)
    assert summary["sent"] == 0
    assert summary["blocked"] == 1
    assert summary["results"][0]["error"] == "unsafe_content"
    assert boom["hit"] is False  # never even attempted delivery


# ===========================================================================
# 12. Unverified email blocks even when approved
# ===========================================================================

def test_unverified_email_blocks_even_when_approved(integration_mode):
    ws = _ws("osp")
    _approved_chain(ws, verified=False)
    summary = send_approved_once(limit=10)
    assert summary["sent"] == 0
    assert summary["blocked"] == 1
    assert summary["results"][0]["error"] == "email_unverified"


# ===========================================================================
# 13. Duplicate send blocks even when approved
# ===========================================================================

def test_duplicate_send_blocks_even_when_approved(integration_mode):
    ws = _ws("osp")
    _approved_chain(ws, delivered=True)  # engine content already delivered
    summary = send_approved_once(limit=10)
    assert summary["sent"] == 0
    assert summary["blocked"] == 1
    assert summary["results"][0]["error"] == "already_sent"


# ===========================================================================
# 14. API endpoints still work
# ===========================================================================

def test_api_endpoints_still_work(monkeypatch):
    from src.api.server import app
    c = TestClient(app)
    # Public health endpoint.
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "osp-gtm-enrichment"
    # Auth still enforced on the process endpoint.
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    r2 = c.post("/api/v1/leads/process", json={"leads": [{}]})
    assert r2.status_code == 401


# ===========================================================================
# 15. Docker docs include the SalesOS worker commands
# ===========================================================================

def test_docker_docs_include_salesos_workers():
    worker = "src.integrations.salesos.worker"
    send = "src.integrations.salesos.send_approved"
    for name in ("Dockerfile", "docker-compose.yml", "README.md"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert worker in text, f"{name} must reference {worker}"
        assert send in text, f"{name} must reference {send}"


# ===========================================================================
# 16. Streamlit files / pages are not removed
# ===========================================================================

def test_streamlit_ui_preserved():
    assert (_ROOT / "app" / "main.py").exists(), "Streamlit entrypoint must remain"
    pages = list((_ROOT / "app" / "pages").glob("*.py"))
    assert pages, "Streamlit pages must remain as the internal admin/fallback UI"
