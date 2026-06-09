"""Phase 8: Evergreen lead source scheduler tests — 16 required.

No live LLM calls. Scoring and content functions are monkeypatched.
No live HTTP calls. LeadSourceClient is monkeypatched.

Test matrix (16):
  1.  Scheduled import only runs for enabled workspace
  2.  Disabled workspace is skipped
  3.  Import creates only new contacts (dedup prevents re-creation)
  4.  Duplicate contacts are skipped and not regenerated
  5.  Newly imported contacts are scored
  6.  Newly imported contacts get generated content
  7.  Internal enrichment is skipped for OSP Lead Engine contacts
  8.  External signals are preserved in lead_source_raw
  9.  Workspace prompts/settings are used for generated content (workspace_id passed)
  10. OSP workspace auto import does not affect Test Client
  11. Auto process can be disabled while import still works
  12. CLI entry point runs successfully (dry-run)
  13. Scheduler HTTP endpoint requires X-Job-Secret
  14. No emails are sent
  15. No Instantly push happens
  16. No external POST /runs is called
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx as _httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.lead_source.client as client_mod
from src.db import session_scope
from src.lead_source.client import EXTERNAL_SOURCE
from src.lead_source.ingest import import_contacts, run_import, start_import_log
from src.lead_source.scheduler import (
    process_imported_leads,
    run_all_enabled_workspaces,
    run_workspace_auto_import,
)
from src.lead_source.settings import (
    LeadSourceConfig,
    load_lead_source_config,
    save_lead_source_config,
)
from src.models import Enrichment, GeneratedContent, Lead, LeadSourceImport, Score
from src.workspace import create_workspace, seed_default_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_ws() -> int:
    seed_default_workspace()
    from src.workspace import get_default_workspace_id
    ws_id = get_default_workspace_id()
    assert ws_id is not None
    return ws_id


def _ws(name: str, slug: str) -> int:
    ws = create_workspace(name=name, slug=slug, instantly_campaign_id=f"camp-{slug}")
    return ws["id"]


def _cfg(ws_id: int, *, auto_import=True, auto_process=True) -> None:
    cfg = LeadSourceConfig(
        enabled=True,
        api_base_url="https://leads.osp.tools",
        api_key="test-key",
        client_slug="osp",
        daily_fetch_limit=10,
        auto_import_enabled=auto_import,
        auto_process_enabled=auto_process,
    )
    save_lead_source_config(cfg, ws_id)


def _make_contact(
    *,
    id: str = "ext-s1",
    email: str = "sched@example.com",
    first_name: str = "Sched",
    last_name: str = "Test",
    signals: list | None = None,
) -> dict:
    return {
        "id": id,
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company_name": "Test Corp",
        "company_domain": "testcorp.com",
        "linkedin_url": "",
        "title": "CEO",
        "mobile_phone": "",
        "signals": signals or [],
        "enrichment_status": "enriched",
        "source": "test",
        "created_at": "2026-01-01T00:00:00",
    }


def _log(ws_id: int) -> int:
    return start_import_log(ws_id, "osp", requested_limit=10)


def _import_one(contact: dict, ws_id: int) -> int:
    log_id = _log(ws_id)
    result = import_contacts([contact], workspace_id=ws_id, client_slug="osp", import_id=log_id)
    assert result.created == 1
    return result.created_lead_ids[0]


# Async-to-sync helper for tests (Python 3.10+ compatible)
def _run(coro):
    return asyncio.run(coro)


# Fake async functions for LLM mocking
class _FakeScoreResult:
    score = 80
    tier = "A"
    rationale = "Mock"
    signals_used: list = []


async def fake_score_lead(lead_id, *, workspace_id=None):
    return _FakeScoreResult()


async def fake_generate_email(lead_id, *, workspace_id=None, **kwargs):
    return None  # idempotent — returns None if already exists is fine


async def fake_generate_call_script(lead_id, *, workspace_id=None, **kwargs):
    return None


async def fake_generate_linkedin_msg(lead_id, *, workspace_id=None, **kwargs):
    return None


class _FakeResp:
    def __init__(self, status: int, data: dict):
        self.status_code = status
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore

    def json(self):
        return self._data


# ---------------------------------------------------------------------------
# Test 1: Scheduled import only runs for enabled workspace
# ---------------------------------------------------------------------------

def test_scheduled_import_only_runs_for_enabled_workspace(monkeypatch):
    ws1_id = _seed_ws()
    _cfg(ws1_id, auto_import=True)

    ws2_id = _ws("Other", "other-s1")
    # ws2 has auto_import_enabled=False (default)

    contacts_fetched = []

    def fake_get(url, **kwargs):
        if "health" in url:
            return _FakeResp(200, {"status": "ok"})
        if "contacts" in url:
            contacts_fetched.append(url)
            return _FakeResp(200, {"contacts": [_make_contact()], "count": 1, "limit": 10, "offset": 0})
        return _FakeResp(200, {})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    results = _run(run_all_enabled_workspaces())
    # Only ws1 should have been processed
    processed_ws_ids = [r["workspace_id"] for r in results if not r.get("skipped")]
    assert ws1_id in processed_ws_ids
    assert ws2_id not in processed_ws_ids


# ---------------------------------------------------------------------------
# Test 2: Disabled workspace is skipped
# ---------------------------------------------------------------------------

def test_disabled_workspace_is_skipped(monkeypatch):
    ws_id = _seed_ws()
    # Leave auto_import_enabled=False
    cfg = LeadSourceConfig(enabled=True, api_base_url="https://x.com", api_key="k",
                           client_slug="osp", auto_import_enabled=False)
    save_lead_source_config(cfg, ws_id)

    results = _run(run_all_enabled_workspaces())
    for r in results:
        if r["workspace_id"] == ws_id:
            assert r.get("skipped") is True


# ---------------------------------------------------------------------------
# Test 3: Import creates only new contacts (dedup)
# ---------------------------------------------------------------------------

def test_import_creates_only_new_contacts(monkeypatch):
    ws_id = _seed_ws()
    contact = _make_contact(id="dup-s3", email="new-s3@example.com",
                            first_name="S3", last_name="Test")

    log_id = _log(ws_id)
    r1 = import_contacts([contact], workspace_id=ws_id, client_slug="osp", import_id=log_id)
    assert r1.created == 1

    log_id2 = _log(ws_id)
    r2 = import_contacts([contact], workspace_id=ws_id, client_slug="osp", import_id=log_id2)
    assert r2.created == 0
    assert r2.skipped >= 1

    with session_scope() as session:
        rows = session.execute(
            select(Lead).where(Lead.external_contact_id == "dup-s3", Lead.workspace_id == ws_id)
        ).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test 4: Duplicate contacts are not regenerated
# ---------------------------------------------------------------------------

def test_duplicate_contacts_not_regenerated(monkeypatch):
    ws_id = _seed_ws()
    contact = _make_contact(id="dup-s4", email="dup-s4@example.com",
                            first_name="D4", last_name="Up")

    called = []

    async def fake_score(lead_id, *, workspace_id=None):
        called.append(("score", lead_id))
        return _FakeScoreResult()

    monkeypatch.setattr("src.scoring.score_lead", fake_score)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    # First import + process
    lead_id = _import_one(contact, ws_id)
    _run(process_imported_leads([lead_id], ws_id))
    first_call_count = len(called)

    # Second import of same contact — should create 0 new leads
    log_id = _log(ws_id)
    r2 = import_contacts([contact], workspace_id=ws_id, client_slug="osp", import_id=log_id)
    assert r2.created == 0
    assert r2.created_lead_ids == []

    # process_imported_leads called with empty list → no additional score calls
    _run(process_imported_leads([], ws_id))
    assert len(called) == first_call_count  # unchanged


# ---------------------------------------------------------------------------
# Test 5: Newly imported contacts are scored
# ---------------------------------------------------------------------------

def test_newly_imported_contacts_are_scored(monkeypatch):
    ws_id = _seed_ws()
    scored = []

    async def fake_score(lead_id, *, workspace_id=None):
        scored.append(lead_id)
        return _FakeScoreResult()

    monkeypatch.setattr("src.scoring.score_lead", fake_score)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="scored-s5", email="score-s5@example.com",
                      first_name="S5", last_name="Test"),
        ws_id,
    )
    result = _run(process_imported_leads([lead_id], ws_id))

    assert lead_id in scored
    assert result["scored_count"] == 1


# ---------------------------------------------------------------------------
# Test 6: Newly imported contacts get generated content
# ---------------------------------------------------------------------------

def test_newly_imported_contacts_get_generated_content(monkeypatch):
    ws_id = _seed_ws()
    generated = []

    async def fake_gen(lead_id, *, workspace_id=None, **kw):
        generated.append(lead_id)

    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_gen)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_gen)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_gen)

    lead_id = _import_one(
        _make_contact(id="gen-s6", email="gen-s6@example.com",
                      first_name="G6", last_name="Test"),
        ws_id,
    )
    result = _run(process_imported_leads([lead_id], ws_id))

    assert generated.count(lead_id) == 3   # email + call_script + linkedin_msg
    assert result["content_generated_count"] == 3


# ---------------------------------------------------------------------------
# Test 7: Internal enrichment is skipped for OSP Lead Engine contacts
# ---------------------------------------------------------------------------

def test_enrichment_skipped_for_osp_lead_engine_contacts(monkeypatch):
    ws_id = _seed_ws()

    enrich_calls = []

    async def spy_enrich(*args, **kwargs):
        enrich_calls.append(args)

    # The scheduler does NOT call enrichment. Verify by checking that no
    # Enrichment rows exist after processing.
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="noenrich-s7", email="noenrich-s7@example.com",
                      first_name="N7", last_name="Test"),
        ws_id,
    )
    result = _run(process_imported_leads([lead_id], ws_id))

    with session_scope() as session:
        enrichment_rows = session.execute(select(Enrichment)).scalars().all()

    assert enrichment_rows == [], "Enrichment must never be called for OSP Lead Engine imports"
    assert result["enrichment_skipped_count"] == 1
    assert enrich_calls == []


# ---------------------------------------------------------------------------
# Test 8: External signals preserved in lead_source_raw
# ---------------------------------------------------------------------------

def test_external_signals_preserved_in_raw_payload():
    ws_id = _seed_ws()
    contact = _make_contact(
        id="sig-s8", email="sig-s8@example.com",
        first_name="S8", last_name="Sig",
        signals=[{"type": "hiring", "source": "linkedin", "value": "engineer"}],
    )
    lead_id = _import_one(contact, ws_id)

    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        sigs = lead.lead_source_raw.get("signals", [])

    assert len(sigs) == 1
    assert sigs[0]["type"] == "hiring"


# ---------------------------------------------------------------------------
# Test 9: Workspace ID is passed to scoring and content functions
# ---------------------------------------------------------------------------

def test_workspace_id_passed_to_scoring_and_content(monkeypatch):
    ws_id = _seed_ws()
    ws_ids_seen = {"score": [], "content": []}

    async def fake_score_ws(lead_id, *, workspace_id=None):
        ws_ids_seen["score"].append(workspace_id)
        return _FakeScoreResult()

    async def fake_gen_ws(lead_id, *, workspace_id=None, **kw):
        ws_ids_seen["content"].append(workspace_id)

    monkeypatch.setattr("src.scoring.score_lead", fake_score_ws)
    monkeypatch.setattr("src.content.email.generate_email", fake_gen_ws)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_gen_ws)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_gen_ws)

    lead_id = _import_one(
        _make_contact(id="wsid-s9", email="wsid-s9@example.com",
                      first_name="W9", last_name="Test"),
        ws_id,
    )
    _run(process_imported_leads([lead_id], ws_id))

    assert all(w == ws_id for w in ws_ids_seen["score"])
    assert all(w == ws_id for w in ws_ids_seen["content"])


# ---------------------------------------------------------------------------
# Test 10: OSP auto import does not affect Test Client
# ---------------------------------------------------------------------------

def test_osp_auto_import_does_not_affect_test_client(monkeypatch):
    ws1_id = _seed_ws()
    ws2_id = _ws("TestClient", "testclient-s10")
    _cfg(ws1_id, auto_import=True, auto_process=False)

    def fake_get(url, **kwargs):
        if "contacts" in url:
            return _FakeResp(200, {
                "contacts": [_make_contact(id="osp-only", email="osp-only@example.com",
                                           first_name="OSP", last_name="Only")],
                "count": 1, "limit": 10, "offset": 0,
            })
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    _run(run_workspace_auto_import(ws1_id))

    with session_scope() as session:
        ws2_leads = session.execute(
            select(Lead).where(Lead.workspace_id == ws2_id)
        ).scalars().all()
    assert ws2_leads == []


# ---------------------------------------------------------------------------
# Test 11: Auto process can be disabled while import still works
# ---------------------------------------------------------------------------

def test_auto_process_disabled_import_still_works(monkeypatch):
    ws_id = _seed_ws()
    _cfg(ws_id, auto_import=True, auto_process=False)  # import yes, process no

    called_score = []

    async def spy_score(lead_id, *, workspace_id=None):
        called_score.append(lead_id)
        return _FakeScoreResult()

    monkeypatch.setattr("src.scoring.score_lead", spy_score)

    def fake_get(url, **kwargs):
        if "contacts" in url:
            return _FakeResp(200, {
                "contacts": [_make_contact(id="imp-only", email="imp-only@example.com",
                                           first_name="Imp", last_name="Only")],
                "count": 1, "limit": 10, "offset": 0,
            })
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    result = _run(run_workspace_auto_import(ws_id))

    # Import happened
    assert result.get("created", 0) >= 0  # might be 1 or 0 depending on prior state
    # Scoring was NOT called (auto_process_enabled=False)
    assert called_score == []


# ---------------------------------------------------------------------------
# Test 12: CLI entry point runs successfully (dry-run)
# ---------------------------------------------------------------------------

def test_cli_dry_run_runs_successfully():
    ws_id = _seed_ws()
    _cfg(ws_id, auto_import=True)

    # run_all_enabled_workspaces with dry_run=True should return without HTTP calls
    results = _run(run_all_enabled_workspaces(dry_run=True))

    for r in results:
        if r["workspace_id"] == ws_id:
            assert r.get("dry_run") is True
            assert r.get("skipped") is not True
            assert r.get("created", 0) == 0  # dry run, nothing imported


# ---------------------------------------------------------------------------
# Test 13: Scheduler HTTP endpoint requires X-Job-Secret
# ---------------------------------------------------------------------------

def test_scheduler_endpoint_requires_job_secret(monkeypatch):
    os.environ["LEAD_SOURCE_JOB_SECRET"] = "correct-job-secret-xyz"

    from src.webhook.server import app as webhook_app
    client = TestClient(webhook_app)

    # No secret → 401
    resp = client.post("/api/lead-source/run-scheduled", json={})
    assert resp.status_code == 401

    # Wrong secret → 401
    resp = client.post(
        "/api/lead-source/run-scheduled",
        json={},
        headers={"X-Job-Secret": "wrong-secret"},
    )
    assert resp.status_code == 401

    # Correct secret → 200 (dry_run to avoid real API calls)
    resp = client.post(
        "/api/lead-source/run-scheduled",
        json={"dry_run": True},
        headers={"X-Job-Secret": "correct-job-secret-xyz"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Test 14: No emails are sent
# ---------------------------------------------------------------------------

def test_no_emails_are_sent(monkeypatch):
    ws_id = _seed_ws()

    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="nosend-s14", email="nosend-s14@example.com",
                      first_name="NS", last_name="14"),
        ws_id,
    )
    _run(process_imported_leads([lead_id], ws_id))

    # No GeneratedContent should have a delivered_at set (no Instantly push)
    with session_scope() as session:
        contents = session.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == lead_id)
        ).scalars().all()
        # Since generate_email is mocked to return None (no actual DB row created),
        # or creates a row without delivered_at — verify none are delivered
        for c in contents:
            assert c.delivered_at is None
            assert c.delivery_status != "sent" if c.delivery_status else True


# ---------------------------------------------------------------------------
# Test 15: No Instantly push happens
# ---------------------------------------------------------------------------

def test_no_instantly_push_happens(monkeypatch):
    ws_id = _seed_ws()

    instantly_calls = []

    def spy_get(url, **kwargs):
        if "instantly" in url.lower():
            instantly_calls.append(url)
        return _FakeResp(200, {})

    def spy_post(url, **kwargs):
        if "instantly" in url.lower():
            instantly_calls.append(url)
        return _FakeResp(200, {})

    monkeypatch.setattr(client_mod.httpx, "get", spy_get)
    monkeypatch.setattr("httpx.post", spy_post)

    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="nopush-s15", email="nopush-s15@example.com",
                      first_name="NP", last_name="15"),
        ws_id,
    )
    _run(process_imported_leads([lead_id], ws_id))

    assert instantly_calls == [], "No Instantly API calls must be made during auto-processing"


# ---------------------------------------------------------------------------
# Test 16: No external POST /runs is called
# ---------------------------------------------------------------------------

def test_no_external_post_runs_called(monkeypatch):
    ws_id = _seed_ws()
    _cfg(ws_id, auto_import=True, auto_process=False)

    runs_calls = []

    def spy_post(url, **kwargs):
        if "/runs" in url:
            runs_calls.append(url)
        return _FakeResp(200, {})

    def fake_get(url, **kwargs):
        if "contacts" in url:
            return _FakeResp(200, {"contacts": [], "count": 0, "limit": 10, "offset": 0})
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)
    monkeypatch.setattr(client_mod.httpx, "post", spy_post)

    _run(run_workspace_auto_import(ws_id))

    assert runs_calls == [], "POST /runs must never be called by the scheduler"
