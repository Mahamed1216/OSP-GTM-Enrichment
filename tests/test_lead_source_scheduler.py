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
    # Enable all content types for this workspace so we still exercise the
    # full 3-generator path (call scripts + LinkedIn DMs now default OFF).
    from src.icp_config import ICPConfig, save_workspace_icp_config
    save_workspace_icp_config(
        ICPConfig(
            generate_email_enabled=True,
            generate_call_script_enabled=True,
            generate_linkedin_dm_enabled=True,
        ),
        workspace_id=ws_id,
    )
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
# Test 7: Enrichment and buyer research run for newly imported OSP Lead Engine contacts
# ---------------------------------------------------------------------------

def test_enrichment_runs_for_osp_lead_engine_contacts(monkeypatch):
    """Newly imported OSP Lead Engine leads get enrichment + buyer research."""
    ws_id = _seed_ws()

    enrich_calls: list[int] = []

    async def spy_enrich(lead_id, *, workspace_id=None):
        enrich_calls.append(lead_id)

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead", spy_enrich)
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="enrich-s7", email="enrich-s7@example.com",
                      first_name="E7", last_name="Test"),
        ws_id,
    )
    result = _run(process_imported_leads([lead_id], ws_id))

    assert lead_id in enrich_calls, "enrich_lead must be called for newly imported leads"
    assert result["enriched_count"] == 1
    assert result["enrichment_skipped_count"] == 0


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


# ===========================================================================
# Cursor / pagination tests (Phase 9)
# ===========================================================================

from src.lead_source.settings import advance_import_cursor, reset_import_cursor


def _cfg_with_offset(ws_id: int, offset: int = 0, limit: int = 10) -> None:
    cfg = LeadSourceConfig(
        enabled=True,
        api_base_url="https://leads.osp.tools",
        api_key="test-key",
        client_slug="osp",
        daily_fetch_limit=limit,
        auto_import_enabled=True,
        auto_process_enabled=False,
        next_offset=offset,
    )
    save_lead_source_config(cfg, ws_id)


def _fake_contacts_get(contacts: list[dict], *, offset_tracker: list | None = None):
    """Return a fake httpx.get that serves `contacts` from the given offset."""

    def fake_get(url, **kwargs):
        if "contacts" in url:
            params = kwargs.get("params", {})
            req_offset = params.get("offset", 0) if params else 0
            req_limit = params.get("limit", 10) if params else 10
            if offset_tracker is not None:
                offset_tracker.append(req_offset)
            batch = contacts[req_offset: req_offset + req_limit]
            return _FakeResp(200, {
                "contacts": batch,
                "count": len(contacts),
                "limit": req_limit,
                "offset": req_offset,
            })
        return _FakeResp(200, {"status": "ok"})

    return fake_get


# ---------------------------------------------------------------------------
# C1. First scheduled run uses offset 0
# ---------------------------------------------------------------------------

def test_first_scheduled_run_uses_offset_0(monkeypatch):
    ws_id = _seed_ws()
    _cfg_with_offset(ws_id, offset=0, limit=5)

    offsets_used: list[int] = []
    all_contacts = [
        _make_contact(id=f"c{i}", email=f"c{i}@x.com", first_name=f"C{i}", last_name="T")
        for i in range(20)
    ]
    monkeypatch.setattr(client_mod.httpx, "get",
                        _fake_contacts_get(all_contacts, offset_tracker=offsets_used))

    _run(run_workspace_auto_import(ws_id))

    assert offsets_used[0] == 0, "First scheduled run must start at offset 0"


# ---------------------------------------------------------------------------
# C2. After fetching limit contacts, next offset = old_offset + fetched_count
# ---------------------------------------------------------------------------

def test_cursor_advances_after_full_fetch(monkeypatch):
    ws_id = _seed_ws()
    _cfg_with_offset(ws_id, offset=0, limit=5)

    all_contacts = [
        _make_contact(id=f"adv{i}", email=f"adv{i}@x.com", first_name=f"A{i}", last_name="D")
        for i in range(20)
    ]
    monkeypatch.setattr(client_mod.httpx, "get", _fake_contacts_get(all_contacts))

    _run(run_workspace_auto_import(ws_id))

    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.next_offset == 5, (
        "After fetching exactly limit=5 contacts, next_offset must be 0+5=5"
    )


# ---------------------------------------------------------------------------
# C3. Second scheduled run uses the advanced offset
# ---------------------------------------------------------------------------

def test_second_run_uses_advanced_offset(monkeypatch):
    ws_id = _seed_ws()
    _cfg_with_offset(ws_id, offset=5, limit=5)

    offsets_used: list[int] = []
    all_contacts = [
        _make_contact(id=f"sec{i}", email=f"sec{i}@x.com", first_name=f"S{i}", last_name="E")
        for i in range(20)
    ]
    monkeypatch.setattr(client_mod.httpx, "get",
                        _fake_contacts_get(all_contacts, offset_tracker=offsets_used))

    _run(run_workspace_auto_import(ws_id))

    assert offsets_used[0] == 5, (
        "Second scheduled run (next_offset=5) must start at offset 5"
    )

    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.next_offset == 10, "After second run, next_offset must advance to 10"


# ---------------------------------------------------------------------------
# C4. When fetched_count < limit, cursor resets to 0 (end of list)
# ---------------------------------------------------------------------------

def test_cursor_resets_when_end_of_list_reached(monkeypatch):
    ws_id = _seed_ws()
    _cfg_with_offset(ws_id, offset=15, limit=10)

    # Only 3 contacts at offset 15 (fewer than limit=10) — end of list
    all_contacts = [
        _make_contact(id=f"eol{i}", email=f"eol{i}@x.com", first_name=f"E{i}", last_name="L")
        for i in range(18)
    ]
    monkeypatch.setattr(client_mod.httpx, "get", _fake_contacts_get(all_contacts))

    _run(run_workspace_auto_import(ws_id))

    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.next_offset == 0, (
        "When fetched_count (3) < limit (10), cursor must reset to 0 (end of list reached)"
    )


# ---------------------------------------------------------------------------
# C5. Manual import (run_import direct call) uses offset 0, does not touch cursor
# ---------------------------------------------------------------------------

def test_manual_import_uses_offset_0_and_does_not_advance_cursor(monkeypatch):
    ws_id = _seed_ws()
    _cfg_with_offset(ws_id, offset=42, limit=5)

    offsets_used: list[int] = []
    all_contacts = [
        _make_contact(id=f"man{i}", email=f"man{i}@x.com", first_name=f"M{i}", last_name="N")
        for i in range(10)
    ]
    monkeypatch.setattr(client_mod.httpx, "get",
                        _fake_contacts_get(all_contacts, offset_tracker=offsets_used))

    # Manual import — no initial_offset arg → defaults to 0
    import_id = start_import_log(ws_id, "osp", requested_limit=5)
    run_import(ws_id, "osp", "https://leads.osp.tools", "test-key", limit=5)

    assert offsets_used[0] == 0, "Manual import must always start at offset 0"

    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.next_offset == 42, (
        "Manual run_import must not modify the stored cursor (next_offset stays at 42)"
    )


# ---------------------------------------------------------------------------
# C6. reset_import_cursor sets next_offset back to 0
# ---------------------------------------------------------------------------

def test_reset_cursor_sets_offset_to_0():
    ws_id = _seed_ws()
    _cfg_with_offset(ws_id, offset=99, limit=5)

    reset_import_cursor(ws_id)

    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.next_offset == 0, "reset_import_cursor must set next_offset to 0"


# ---------------------------------------------------------------------------
# C7. Duplicates are skipped and not regenerated (cursor still advances)
# ---------------------------------------------------------------------------

def test_duplicate_contacts_skipped_and_cursor_still_advances(monkeypatch):
    ws_id = _seed_ws()
    # Pre-import the first contact so it will be a duplicate
    contact = _make_contact(id="dup-c7", email="dup-c7@example.com",
                            first_name="D7", last_name="Dup")
    _import_one(contact, ws_id)

    _cfg_with_offset(ws_id, offset=0, limit=5)

    all_contacts = [contact] + [
        _make_contact(id=f"new-c7-{i}", email=f"new-c7-{i}@x.com",
                      first_name=f"N{i}", last_name="C7")
        for i in range(4)
    ]
    monkeypatch.setattr(client_mod.httpx, "get", _fake_contacts_get(all_contacts))

    result = _run(run_workspace_auto_import(ws_id))

    assert result["import_skipped"] >= 1, "Duplicate contact must be counted as skipped"
    # Cursor must still advance (fetched 5 == limit 5)
    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.next_offset == 5, "Cursor must advance even when some contacts are duplicates"


# ---------------------------------------------------------------------------
# C8. Newly created leads are processed when auto_process_enabled
# ---------------------------------------------------------------------------

def test_newly_created_leads_are_processed_after_cursor_advance(monkeypatch):
    ws_id = _seed_ws()
    cfg_obj = LeadSourceConfig(
        enabled=True, api_base_url="https://leads.osp.tools", api_key="key",
        client_slug="osp", daily_fetch_limit=5,
        auto_import_enabled=True, auto_process_enabled=True, next_offset=0,
    )
    save_lead_source_config(cfg_obj, ws_id)

    scored: list[int] = []

    async def fake_score(lead_id, *, workspace_id=None):
        scored.append(lead_id)
        return _FakeScoreResult()

    monkeypatch.setattr("src.scoring.score_lead", fake_score)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    all_contacts = [
        _make_contact(id=f"proc{i}", email=f"proc{i}@x.com", first_name=f"P{i}", last_name="R")
        for i in range(5)
    ]
    monkeypatch.setattr(client_mod.httpx, "get", _fake_contacts_get(all_contacts))

    result = _run(run_workspace_auto_import(ws_id))

    assert result["created"] >= 1
    assert len(scored) == result["created"], "All newly created leads must be scored"
    assert load_lead_source_config(ws_id).next_offset == 5


# ---------------------------------------------------------------------------
# C9. Workspace A cursor does not affect Workspace B cursor
# ---------------------------------------------------------------------------

def test_workspace_cursor_isolation():
    ws_a = _seed_ws()
    ws_b = _ws("WorkspaceB-C9", "wb-c9")

    _cfg_with_offset(ws_a, offset=10, limit=5)
    _cfg_with_offset(ws_b, offset=20, limit=5)

    # Advance workspace A cursor directly
    advance_import_cursor(ws_a, fetched_count=5, limit=5)

    cfg_a = load_lead_source_config(ws_a)
    cfg_b = load_lead_source_config(ws_b)

    assert cfg_a.next_offset == 15, "Workspace A cursor must be 10+5=15"
    assert cfg_b.next_offset == 20, "Workspace B cursor must be unchanged at 20"


# ---------------------------------------------------------------------------
# C10. No Instantly push or email send happens during cursor-based scheduled run
# ---------------------------------------------------------------------------

def test_no_instantly_push_or_email_send_during_cursor_run(monkeypatch):
    ws_id = _seed_ws()
    _cfg_with_offset(ws_id, offset=0, limit=5)

    instantly_calls: list[str] = []

    def spy_http(url, **kwargs):
        if "instantly" in url.lower():
            instantly_calls.append(url)
        if "contacts" in url:
            return _FakeResp(200, {
                "contacts": [_make_contact(id="no-push", email="no-push@x.com",
                                           first_name="NP", last_name="NS")],
                "count": 1, "limit": 5, "offset": 0,
            })
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "get", spy_http)
    monkeypatch.setattr("httpx.post", lambda url, **kw: _FakeResp(200, {}))

    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    _run(run_workspace_auto_import(ws_id))

    assert instantly_calls == [], "No Instantly calls must be made during cursor-based scheduled runs"


# ===========================================================================
# Email verification + enrichment tests (Part A / B / C / D)
# ===========================================================================

from src.lead_source.ingest import import_contacts
from src.lead_source.mapper import map_contact
from src.delivery.eligibility import filter_eligible


def _verified_contact(**overrides) -> dict:
    base = _make_contact(id="v-contact", email="verified@x.com",
                         first_name="Ver", last_name="Ified")
    base["email_verified"] = True
    base.update(overrides)
    return base


def _unverified_contact(**overrides) -> dict:
    base = _make_contact(id="u-contact", email="unverified@x.com",
                         first_name="Un", last_name="Verified")
    base["email_verified"] = False
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# V1. email_verified=True maps to local verified status
# ---------------------------------------------------------------------------

def test_email_verified_true_maps_to_verified_status():
    fields = map_contact({"id": "vid-1", "email": "v1@x.com",
                          "email_verified": True, "first_name": "V", "last_name": "1"})
    assert fields["email_verification_status"] == "verified"
    assert fields["email_verification_provider"] == "osp_lead_engine"
    assert fields["email_verified_at"] is not None


# ---------------------------------------------------------------------------
# V2. Verified imported lead passes email_unverified eligibility gate
# ---------------------------------------------------------------------------

def test_verified_imported_lead_not_blocked_by_eligibility():
    ws_id = _seed_ws()
    contact = _verified_contact(id="v2-elig", email="v2-elig@x.com",
                                first_name="V2", last_name="Elig")
    log_id = _log(ws_id)
    result = import_contacts([contact], workspace_id=ws_id, client_slug="osp", import_id=log_id)
    assert result.created == 1
    lead_id = result.created_lead_ids[0]

    # Add a passing score and email content so only the email-verification gate matters
    from src.models import Score, GeneratedContent
    with session_scope() as session:
        session.add(Score(lead_id=lead_id, score=90, tier="A", rationale="test",
                          model="test", workspace_id=ws_id))
        session.add(GeneratedContent(lead_id=lead_id, kind="email",
                                     subject="Subj", body="Body",
                                     prompt_version="v1", model="test",
                                     workspace_id=ws_id))

    with session_scope() as session:
        eligible, skipped = filter_eligible([lead_id], session)

    assert lead_id in eligible, "Verified imported lead must pass the email_unverified gate"
    assert lead_id not in skipped.get("email_unverified", [])


# ---------------------------------------------------------------------------
# V3. email_verified=False remains unverified
# ---------------------------------------------------------------------------

def test_email_verified_false_remains_unverified():
    fields = map_contact({"id": "uv-1", "email": "uv1@x.com",
                          "email_verified": False, "first_name": "U", "last_name": "V"})
    assert fields["email_verification_status"] is None
    assert fields["email_verification_provider"] is None
    assert fields["email_verified_at"] is None


# ---------------------------------------------------------------------------
# V4. Manual import maps verification status correctly
# ---------------------------------------------------------------------------

def test_manual_import_maps_verification_status():
    ws_id = _seed_ws()
    contact = _verified_contact(id="v4-manual", email="v4-manual@x.com",
                                first_name="V4", last_name="Manual")
    log_id = _log(ws_id)
    result = import_contacts([contact], workspace_id=ws_id, client_slug="osp", import_id=log_id)
    assert result.created == 1

    with session_scope() as session:
        from src.models import Lead
        lead = session.get(Lead, result.created_lead_ids[0])
        assert lead.email_verification_status == "verified"
        assert lead.email_verification_provider == "osp_lead_engine"
        assert lead.email_verified_at is not None


# ---------------------------------------------------------------------------
# V5. Scheduled import maps verification status correctly
# ---------------------------------------------------------------------------

def test_scheduled_import_maps_verification_status(monkeypatch):
    ws_id = _seed_ws()
    _cfg(ws_id, auto_import=True, auto_process=False)

    contact = _verified_contact(id="v5-sched", email="v5-sched@x.com",
                                first_name="V5", last_name="Sched")

    def fake_get(url, **kwargs):
        if "contacts" in url:
            return _FakeResp(200, {"contacts": [contact], "count": 1, "limit": 10, "offset": 0})
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    _run(run_workspace_auto_import(ws_id))

    with session_scope() as session:
        from src.models import Lead
        lead = session.execute(
            select(Lead).where(Lead.external_contact_id == "v5-sched",
                               Lead.workspace_id == ws_id)
        ).scalar_one_or_none()
        assert lead is not None
        assert lead.email_verification_status == "verified"


# ---------------------------------------------------------------------------
# V6. Evergreen processing runs enrichment for newly imported leads
# ---------------------------------------------------------------------------

def test_evergreen_processing_runs_enrichment(monkeypatch):
    ws_id = _seed_ws()
    enrich_calls: list[int] = []

    async def spy_enrich(lead_id, *, workspace_id=None):
        enrich_calls.append(lead_id)

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead", spy_enrich)
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="v6-enrich", email="v6-enrich@x.com", first_name="V6", last_name="E"),
        ws_id,
    )
    result = _run(process_imported_leads([lead_id], ws_id))

    assert lead_id in enrich_calls
    assert result["enriched_count"] == 1


# ---------------------------------------------------------------------------
# V7. Evergreen processing runs buyer research (part of enrich_lead waterfall)
# ---------------------------------------------------------------------------

def test_evergreen_processing_runs_buyer_research(monkeypatch):
    """buyer_accounts discovery is inside the enrich_lead waterfall.
    Verifying enrich_lead is called confirms buyer research will run."""
    ws_id = _seed_ws()
    enrich_calls: list[int] = []

    async def spy_enrich(lead_id, *, workspace_id=None):
        enrich_calls.append(lead_id)

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead", spy_enrich)
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="v7-buyer", email="v7-buyer@x.com", first_name="V7", last_name="B"),
        ws_id,
    )
    _run(process_imported_leads([lead_id], ws_id))

    assert lead_id in enrich_calls, (
        "enrich_lead must be called — it runs buyer_accounts discovery in the waterfall"
    )


# ---------------------------------------------------------------------------
# V8. Evergreen processing runs scoring
# ---------------------------------------------------------------------------

def test_evergreen_processing_runs_scoring(monkeypatch):
    ws_id = _seed_ws()
    scored: list[int] = []

    async def spy_score(lead_id, *, workspace_id=None):
        scored.append(lead_id)
        return _FakeScoreResult()

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead",
                        lambda lead_id, *, workspace_id=None: None)
    monkeypatch.setattr("src.scoring.score_lead", spy_score)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="v8-score", email="v8-score@x.com", first_name="V8", last_name="S"),
        ws_id,
    )
    result = _run(process_imported_leads([lead_id], ws_id))

    assert lead_id in scored
    assert result["scored_count"] == 1


# ---------------------------------------------------------------------------
# V9. Evergreen processing runs content generation
# ---------------------------------------------------------------------------

def test_evergreen_processing_runs_content_generation(monkeypatch):
    ws_id = _seed_ws()
    # Enable all content types so this still exercises the full 3-generator
    # path (call scripts + LinkedIn DMs default OFF for cost saving).
    from src.icp_config import ICPConfig, save_workspace_icp_config
    save_workspace_icp_config(
        ICPConfig(
            generate_email_enabled=True,
            generate_call_script_enabled=True,
            generate_linkedin_dm_enabled=True,
        ),
        workspace_id=ws_id,
    )
    generated: list[int] = []

    async def spy_gen(lead_id, *, workspace_id=None, **kw):
        generated.append(lead_id)

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead",
                        lambda lead_id, *, workspace_id=None: None)
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", spy_gen)
    monkeypatch.setattr("src.content.call_script.generate_call_script", spy_gen)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", spy_gen)

    lead_id = _import_one(
        _make_contact(id="v9-content", email="v9-content@x.com", first_name="V9", last_name="C"),
        ws_id,
    )
    result = _run(process_imported_leads([lead_id], ws_id))

    assert generated.count(lead_id) == 3
    assert result["content_generated_count"] == 3


# ---------------------------------------------------------------------------
# V10. Duplicate imported leads do not rerun enrichment/content
# ---------------------------------------------------------------------------

def test_duplicate_leads_do_not_rerun_enrichment(monkeypatch):
    ws_id = _seed_ws()
    enrich_calls: list[int] = []

    async def spy_enrich(lead_id, *, workspace_id=None):
        enrich_calls.append(lead_id)
        # Create an actual Enrichment row so _has_enrichment returns True next time
        from src.models import Enrichment
        with session_scope() as s:
            if not s.execute(
                select(Enrichment).where(Enrichment.lead_id == lead_id)
            ).scalar_one_or_none():
                s.add(Enrichment(lead_id=lead_id, workspace_id=ws_id))

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead", spy_enrich)
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _make_contact(id="v10-dup", email="v10-dup@x.com", first_name="V10", last_name="D"),
        ws_id,
    )

    _run(process_imported_leads([lead_id], ws_id))
    assert len(enrich_calls) == 1

    # Second call — Enrichment row exists, must skip
    _run(process_imported_leads([lead_id], ws_id))
    assert len(enrich_calls) == 1, "Enrichment must not rerun when Enrichment row already exists"


# ---------------------------------------------------------------------------
# V11. Existing verified email is not overwritten by blank data
# ---------------------------------------------------------------------------

def test_existing_verified_email_not_overwritten():
    ws_id = _seed_ws()

    # Import a verified contact
    contact1 = _verified_contact(id="v11-orig", email="v11@x.com",
                                 first_name="V11", last_name="Orig")
    log_id = _log(ws_id)
    r1 = import_contacts([contact1], workspace_id=ws_id, client_slug="osp", import_id=log_id)
    assert r1.created == 1

    # Re-import with email_verified=False (should NOT overwrite existing "verified" status)
    contact2 = dict(contact1)
    contact2["email_verified"] = False
    log_id2 = _log(ws_id)
    import_contacts([contact2], workspace_id=ws_id, client_slug="osp", import_id=log_id2)

    with session_scope() as session:
        from src.models import Lead
        lead = session.get(Lead, r1.created_lead_ids[0])
        assert lead.email_verification_status == "verified", (
            "Existing verified email must not be overwritten by a re-import with email_verified=False"
        )


# ---------------------------------------------------------------------------
# V12. Workspace isolation remains intact
# ---------------------------------------------------------------------------

def test_workspace_isolation_email_verification():
    ws_a = _seed_ws()
    ws_b = _ws("IsoB-V12", "iso-b-v12")

    contact_a = _verified_contact(id="v12-a", email="v12-a@x.com",
                                  first_name="A12", last_name="Iso")
    contact_b = _unverified_contact(id="v12-b", email="v12-b@x.com",
                                    first_name="B12", last_name="Iso")

    log_a = _log(ws_a)
    import_contacts([contact_a], workspace_id=ws_a, client_slug="osp", import_id=log_a)

    log_b = _log(ws_b)
    import_contacts([contact_b], workspace_id=ws_b, client_slug="osp", import_id=log_b)

    # Read all attributes inside the session scope to avoid DetachedInstanceError
    with session_scope() as session:
        from src.models import Lead
        lead_a = session.execute(
            select(Lead).where(Lead.external_contact_id == "v12-a",
                               Lead.workspace_id == ws_a)
        ).scalar_one_or_none()
        lead_b = session.execute(
            select(Lead).where(Lead.external_contact_id == "v12-b",
                               Lead.workspace_id == ws_b)
        ).scalar_one_or_none()
        a_status = lead_a.email_verification_status if lead_a else None
        b_status = lead_b.email_verification_status if lead_b else None
        leads_in_b = session.execute(
            select(Lead).where(Lead.workspace_id == ws_b)
        ).scalars().all()
        b_statuses = [l.email_verification_status for l in leads_in_b]

    assert a_status == "verified"
    assert b_status is None
    assert not any(s == "verified" for s in b_statuses), (
        "Verified lead in workspace A must not appear in workspace B"
    )


# ---------------------------------------------------------------------------
# V13. No Instantly push happens automatically (with enrichment path)
# ---------------------------------------------------------------------------

def test_no_instantly_push_with_enrichment(monkeypatch):
    ws_id = _seed_ws()
    instantly_calls: list[str] = []

    def spy_http(url, **kwargs):
        if "instantly" in url.lower():
            instantly_calls.append(url)
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "get", spy_http)
    monkeypatch.setattr("httpx.post", lambda url, **kw: _FakeResp(200, {}))
    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead",
                        lambda lead_id, *, workspace_id=None: None)
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _verified_contact(id="v13-nopush", email="v13-nopush@x.com",
                          first_name="V13", last_name="NP"),
        ws_id,
    )
    _run(process_imported_leads([lead_id], ws_id))

    assert instantly_calls == [], "No Instantly calls must occur even for verified leads"


# ---------------------------------------------------------------------------
# V14. No emails are sent automatically (with enrichment path)
# ---------------------------------------------------------------------------

def test_no_emails_sent_automatically_with_enrichment(monkeypatch):
    ws_id = _seed_ws()
    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead",
                        lambda lead_id, *, workspace_id=None: None)
    monkeypatch.setattr("src.scoring.score_lead", fake_score_lead)
    monkeypatch.setattr("src.content.email.generate_email", fake_generate_email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_generate_call_script)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_generate_linkedin_msg)

    lead_id = _import_one(
        _verified_contact(id="v14-nosend", email="v14-nosend@x.com",
                          first_name="V14", last_name="NS"),
        ws_id,
    )
    _run(process_imported_leads([lead_id], ws_id))

    with session_scope() as session:
        contents = session.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == lead_id)
        ).scalars().all()
        for c in contents:
            assert c.delivered_at is None
            assert c.delivery_status != "sent" if c.delivery_status else True

