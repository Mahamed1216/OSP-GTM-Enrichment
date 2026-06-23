"""Tests for the signal-first run trigger/poll flow + the Dele signal payload.

Covers the required matrix for this feature:
  - trigger run before import ENABLED  → POST /runs is called, then import
  - trigger run DISABLED               → normal contacts-polling, no POST /runs
  - poll a completed run THEN imports
  - failed / timeout run does NOT import silently
  - signals array is parsed
  - signal_count is stored
  - source_tier stored separately from local tier (see test_source_signals.py)
  - source signals appear in lead detail data (full Dele shape)
  - source signals are available to scoring (see test_source_signals.py)
  - workspace isolation (see test_source_signals.py)
  - no auto Instantly push / no auto email send (also during a triggered run)

No live HTTP/LLM. httpx + the trigger/poll clock are injected/monkeypatched.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

import src.lead_source.client as client_mod
from src.db import session_scope
from src.lead_source.ingest import import_contacts, start_import_log
from src.lead_source.runs import (
    RunResult,
    classify_run_status,
    trigger_and_poll_run,
)
from src.lead_source.scheduler import process_imported_leads, run_workspace_auto_import
from src.lead_source.settings import (
    LeadSourceConfig,
    load_lead_source_config,
    save_lead_source_config,
)
from src.lead_source.source_signals import parse_source_signals
from src.models import GeneratedContent, Lead, LeadSignal, LeadSourceImport
from src.signals.store import get_source_signal
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_ws() -> int:
    seed_default_workspace()
    ws_id = get_default_workspace_id()
    assert ws_id is not None
    return ws_id


def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, status: int, data: dict):
        self.status_code = status
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx as _httpx
            raise _httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore

    def json(self):
        return self._data


def _contact(id="ext-r1", email="r1@example.com", **extra) -> dict:
    c = {
        "id": id, "email": email, "first_name": "R", "last_name": "1",
        "company_name": "Acme", "company_domain": "acme.com", "title": "CEO",
        "signals": [], "enrichment_status": "enriched", "created_at": "2026-01-01T00:00:00",
    }
    c.update(extra)
    return c


def _dele_contact(id="dele-1", email="dele@acme.com") -> dict:
    """A contact in Dele's confirmed signal payload shape."""
    return _contact(
        id=id, email=email,
        signal_count=2,
        signals=[
            {
                "type": "hiring", "value": "RevOps Manager", "confidence": 0.92,
                "source": "linkedin", "source_url": "https://linkedin.com/jobs/1",
                "detected_at": "2026-06-01T00:00:00",
            },
            {
                "type": "funding", "value": "Series B $40M", "confidence": 0.8,
                "source": "crunchbase", "source_url": "https://cb.com/acme",
                "detected_at": "2026-05-20T00:00:00",
            },
        ],
        matched_icps=["saas-vp"],
        tier="A", tier_score=95,
        source_metadata={"run_id": "abc", "pipeline": "v2"},
    )


def _trigger_cfg(ws_id: int, *, trigger=True, auto_process=False) -> None:
    cfg = LeadSourceConfig(
        enabled=True, api_base_url="https://leads.osp.tools", api_key="k",
        client_slug="osp", daily_fetch_limit=10,
        auto_import_enabled=True, auto_process_enabled=auto_process,
        trigger_run_before_import=trigger,
        run_poll_interval_seconds=1, run_max_wait_seconds=30,
    )
    save_lead_source_config(cfg, ws_id)


class _FakeClock:
    """Deterministic clock: sleep advances time, monotonic reads it."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakeRunClient:
    """A LeadSourceClient stand-in for unit-testing trigger_and_poll_run."""

    def __init__(self, *, trigger_response=None, statuses=None, trigger_exc=None):
        self.trigger_response = (
            trigger_response if trigger_response is not None
            else {"run_id": "r1", "status": "queued"}
        )
        self.statuses = list(statuses or [])
        self.trigger_exc = trigger_exc
        self.trigger_calls = 0
        self.get_calls = 0

    def trigger_run(self, slug):
        self.trigger_calls += 1
        if self.trigger_exc:
            raise self.trigger_exc
        return self.trigger_response

    def get_run(self, run_id):
        self.get_calls += 1
        status = self.statuses.pop(0) if self.statuses else "running"
        return {"run_id": run_id, "status": status}


# ===========================================================================
# Unit: classify_run_status
# ===========================================================================

def test_classify_run_status():
    assert classify_run_status("completed") == "completed"
    assert classify_run_status("SUCCESS") == "completed"
    assert classify_run_status("done") == "completed"
    assert classify_run_status("failed") == "failed"
    assert classify_run_status("error") == "failed"
    assert classify_run_status("cancelled") == "failed"
    assert classify_run_status("running") == "running"
    assert classify_run_status("queued") == "running"
    assert classify_run_status(None) == "running"
    assert classify_run_status("") == "running"


# ===========================================================================
# Unit: trigger_and_poll_run
# ===========================================================================

def test_poll_completed_then_returns():
    c = _FakeRunClient(statuses=["running", "completed"])
    clock = _FakeClock()
    res = trigger_and_poll_run(
        c, "osp", poll_interval_seconds=5, max_wait_seconds=600,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    assert res.completed is True
    assert res.status == "completed"
    assert res.run_id == "r1"
    assert res.polls == 2


def test_poll_failed_does_not_complete():
    c = _FakeRunClient(statuses=["failed"])
    clock = _FakeClock()
    res = trigger_and_poll_run(
        c, "osp", sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    assert res.completed is False
    assert res.status == "failed"


def test_poll_timeout_does_not_complete():
    c = _FakeRunClient(statuses=[])  # always "running"
    clock = _FakeClock()
    res = trigger_and_poll_run(
        c, "osp", poll_interval_seconds=10, max_wait_seconds=30,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    assert res.completed is False
    assert res.status == "timeout"
    assert res.error  # clear timeout message


def test_trigger_exception_is_error():
    c = _FakeRunClient(trigger_exc=RuntimeError("boom"))
    clock = _FakeClock()
    res = trigger_and_poll_run(c, "osp", sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    assert res.completed is False
    assert res.status == "error"
    assert "boom" in (res.error or "")


def test_trigger_without_run_id_is_error():
    c = _FakeRunClient(trigger_response={"status": "queued"})
    clock = _FakeClock()
    res = trigger_and_poll_run(c, "osp", sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    assert res.completed is False
    assert res.status == "error"
    assert c.get_calls == 0


def test_trigger_immediate_completed_skips_polling():
    c = _FakeRunClient(trigger_response={"run_id": "r9", "status": "completed"})
    clock = _FakeClock()
    res = trigger_and_poll_run(c, "osp", sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    assert res.completed is True
    assert res.polls == 0
    assert c.get_calls == 0


def test_poll_transient_error_then_completes():
    class _Flaky(_FakeRunClient):
        def get_run(self, run_id):
            self.get_calls += 1
            if self.get_calls == 1:
                raise RuntimeError("temporary network blip")
            return {"run_id": run_id, "status": "completed"}

    c = _Flaky()
    clock = _FakeClock()
    res = trigger_and_poll_run(
        c, "osp", poll_interval_seconds=5, max_wait_seconds=100,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    assert res.completed is True
    assert c.get_calls == 2


# ===========================================================================
# Scheduler integration: trigger enabled vs disabled
# ===========================================================================

def test_trigger_enabled_posts_run_then_imports(monkeypatch):
    ws_id = _seed_ws()
    _trigger_cfg(ws_id, trigger=True)

    posts: list[str] = []

    def fake_post(url, **kw):
        posts.append(url)
        if url.endswith("/runs"):
            return _FakeResp(200, {"run_id": "run-1", "status": "queued"})
        return _FakeResp(200, {})

    def fake_get(url, **kw):
        if "/runs/" in url:               # poll
            return _FakeResp(200, {"run_id": "run-1", "status": "completed"})
        if "contacts" in url:
            return _FakeResp(200, {"contacts": [_contact()], "count": 1, "limit": 10, "offset": 0})
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "post", fake_post)
    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    result = _run(run_workspace_auto_import(ws_id))

    assert any(u.endswith("/runs") for u in posts), "POST /runs must be called when trigger enabled"
    assert result["triggered_run_id"] == "run-1"
    assert result["triggered_run_status"] == "completed"
    assert result["created"] == 1

    with session_scope() as s:
        imp = s.execute(
            select(LeadSourceImport)
            .where(LeadSourceImport.workspace_id == ws_id)
            .order_by(LeadSourceImport.id.desc())
        ).scalars().first()
        assert imp.triggered_run_id == "run-1"
        assert imp.triggered_run_status == "completed"

    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.last_run_id == "run-1"
    assert cfg_after.last_run_status == "completed"


def test_trigger_disabled_uses_normal_polling(monkeypatch):
    ws_id = _seed_ws()
    _trigger_cfg(ws_id, trigger=False)

    posts: list[str] = []

    def fake_post(url, **kw):
        posts.append(url)
        return _FakeResp(200, {})

    def fake_get(url, **kw):
        if "contacts" in url:
            return _FakeResp(200, {
                "contacts": [_contact(id="np1", email="np1@x.com")],
                "count": 1, "limit": 10, "offset": 0,
            })
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "post", fake_post)
    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    result = _run(run_workspace_auto_import(ws_id))

    assert not any(u.endswith("/runs") for u in posts), "POST /runs must NOT be called when trigger disabled"
    assert result["created"] == 1
    assert result.get("triggered_run_id") is None
    assert result.get("triggered_run_status") is None


def test_failed_run_does_not_import_silently(monkeypatch):
    ws_id = _seed_ws()
    _trigger_cfg(ws_id, trigger=True)

    contacts_fetched: list[str] = []

    def fake_post(url, **kw):
        if url.endswith("/runs"):
            return _FakeResp(200, {"run_id": "run-f", "status": "queued"})
        return _FakeResp(200, {})

    def fake_get(url, **kw):
        if "/runs/" in url:
            return _FakeResp(200, {"run_id": "run-f", "status": "failed", "error": "source pipeline crashed"})
        if "contacts" in url:
            contacts_fetched.append(url)
            return _FakeResp(200, {"contacts": [_contact()], "count": 1, "limit": 10, "offset": 0})
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "post", fake_post)
    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    result = _run(run_workspace_auto_import(ws_id))

    assert contacts_fetched == [], "Contacts must NOT be fetched when the run failed"
    assert result["created"] == 0
    assert result["skipped"] is True
    assert result["triggered_run_status"] == "failed"

    with session_scope() as s:
        leads = s.execute(select(Lead).where(Lead.workspace_id == ws_id)).scalars().all()
        assert leads == [], "No leads may be imported when the triggered run failed"

    cfg_after = load_lead_source_config(ws_id)
    assert cfg_after.last_run_status == "failed"
    assert cfg_after.last_run_error is not None


def test_timeout_run_does_not_import(monkeypatch):
    ws_id = _seed_ws()
    _trigger_cfg(ws_id, trigger=True)

    from src.lead_source import runs as runs_mod

    def fake_trigger_poll(client, slug, **kw):
        return RunResult(
            triggered=True, run_id="run-t", status="timeout",
            completed=False, error="run did not complete within 30s", polls=3,
        )

    monkeypatch.setattr(runs_mod, "trigger_and_poll_run", fake_trigger_poll)

    contacts_fetched: list[str] = []

    def fake_get(url, **kw):
        if "contacts" in url:
            contacts_fetched.append(url)
            return _FakeResp(200, {"contacts": [_contact()], "count": 1, "limit": 10, "offset": 0})
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)
    monkeypatch.setattr(client_mod.httpx, "post", lambda url, **kw: _FakeResp(200, {}))

    result = _run(run_workspace_auto_import(ws_id))

    assert contacts_fetched == [], "Contacts must NOT be fetched when the run timed out"
    assert result["created"] == 0
    assert result["triggered_run_status"] == "timeout"


def test_no_instantly_push_during_triggered_run(monkeypatch):
    ws_id = _seed_ws()
    _trigger_cfg(ws_id, trigger=True, auto_process=True)

    instantly_calls: list[str] = []

    def fake_post(url, **kw):
        if "instantly" in url.lower():
            instantly_calls.append(url)
        if url.endswith("/runs"):
            return _FakeResp(200, {"run_id": "run-x", "status": "queued"})
        return _FakeResp(200, {})

    def fake_get(url, **kw):
        if "instantly" in url.lower():
            instantly_calls.append(url)
        if "/runs/" in url:
            return _FakeResp(200, {"run_id": "run-x", "status": "completed"})
        if "contacts" in url:
            return _FakeResp(200, {
                "contacts": [_contact(id="tr1", email="tr1@x.com")],
                "count": 1, "limit": 10, "offset": 0,
            })
        return _FakeResp(200, {"status": "ok"})

    monkeypatch.setattr(client_mod.httpx, "post", fake_post)
    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    async def fake_score(lead_id, *, workspace_id=None):
        class _R:
            score, tier, rationale, signals_used = 80, "A", "m", []
        return _R()

    async def fake_gen(lead_id, *, workspace_id=None, **kw):
        return None

    monkeypatch.setattr("src.enrichment.waterfall.enrich_lead",
                        lambda lead_id, *, workspace_id=None: None)
    monkeypatch.setattr("src.scoring.score_lead", fake_score)
    monkeypatch.setattr("src.content.email.generate_email", fake_gen)
    monkeypatch.setattr("src.content.call_script.generate_call_script", fake_gen)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", fake_gen)

    _run(run_workspace_auto_import(ws_id))

    assert instantly_calls == [], "No Instantly calls during a triggered run"
    with session_scope() as s:
        contents = s.execute(select(GeneratedContent)).scalars().all()
        for c in contents:
            assert c.delivered_at is None
            assert (c.delivery_status != "sent") if c.delivery_status else True


# ===========================================================================
# Dele signal payload normalization
# ===========================================================================

def test_signal_count_prefers_payload_then_falls_back():
    parsed = parse_source_signals(
        {"id": "x", "signal_count": 3, "signals": [{"type": "a"}, {"type": "b"}]}
    )
    assert parsed.signal_count == 3   # payload's explicit count wins

    parsed2 = parse_source_signals({"id": "x", "signals": [{"type": "a"}]})
    assert parsed2.signal_count == 1  # falls back to len(signals)

    parsed3 = parse_source_signals({"id": "x", "signals": []})
    assert parsed3.signal_count == 0


def test_dele_signal_shape_fully_normalized():
    parsed = parse_source_signals(_dele_contact())
    assert parsed.has_signals is True
    assert parsed.signal_count == 2
    assert parsed.source_tier == "A"
    assert parsed.source_tier_score == 95.0
    assert parsed.matched_icps == ["saas-vp"]
    assert parsed.source_metadata == {"run_id": "abc", "pipeline": "v2"}

    sig = parsed.signals[0]
    assert sig["signal_type"] == "hiring"
    assert sig["signal_value"] == "RevOps Manager"
    assert sig["signal_confidence"] == 0.92
    assert sig["signal_source"] == "linkedin"
    assert sig["signal_source_url"] == "https://linkedin.com/jobs/1"
    assert sig["signal_detected_at"] == "2026-06-01T00:00:00"
    assert sig["signal_strength"] == "high"  # 0.92 → high


def test_signal_count_stored_on_import_log_and_signal_row():
    osp = _seed_ws()
    log_id = start_import_log(osp, "osp", requested_limit=10)
    result = import_contacts([_dele_contact()], workspace_id=osp, client_slug="osp", import_id=log_id)

    assert result.created == 1
    assert result.source_signal_count == 2

    with session_scope() as s:
        imp = s.get(LeadSourceImport, log_id)
        assert imp.source_signal_count == 2
        assert imp.raw_summary["source_signal_count"] == 2

    row = get_source_signal(result.created_lead_ids[0])
    assert row is not None
    assert row["signal_found"] is True

    # The normalized ParsedSourceSignals payload (with signal_count) is stored
    # on the lead_signals row.
    with session_scope() as s:
        sig = s.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == result.created_lead_ids[0],
                LeadSignal.signal_type == "source_import",
            )
        ).scalar_one()
        assert sig.raw_payload["signal_count"] == 2


def test_lead_detail_exposes_full_signal_shape():
    osp = _seed_ws()
    log_id = start_import_log(osp, "osp", requested_limit=10)
    result = import_contacts([_dele_contact()], workspace_id=osp, client_slug="osp", import_id=log_id)
    lead_id = result.created_lead_ids[0]

    from app.lib.db_queries import get_lead_full
    bundle = get_lead_full(lead_id, workspace_id=osp)
    ss = bundle["source_signal"]
    assert ss is not None
    assert ss["signal_count"] == 2
    assert ss["source_metadata"] == {"run_id": "abc", "pipeline": "v2"}
    assert len(ss["signals"]) == 2

    first = ss["signals"][0]
    assert first["signal_type"] == "hiring"
    assert first["signal_value"] == "RevOps Manager"
    assert first["signal_confidence"] == 0.92
    assert first["signal_source"] == "linkedin"
    assert first["signal_detected_at"] == "2026-06-01T00:00:00"
    # Source tier is on the Lead, separate from any local Score tier.
    assert bundle["lead"]["source_tier"] == "A"
