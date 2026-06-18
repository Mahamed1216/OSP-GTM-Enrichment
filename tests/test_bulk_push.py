"""Push-to-Instantly execution (run_bulk_push) — Issue 2 hotfix.

run_bulk_push re-runs server-side eligibility, pushes only still-eligible
selected leads, captures a persistable result, and never swallows errors or
marks blocked/failed leads as sent. deliver is injectable so no network/LLM.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.lib.bulk_push import run_bulk_push
from src.db import session_scope
from src.models import GeneratedContent, Lead, Score, now_utc
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace

CAMP = "camp-osp"


def _seed() -> int:
    seed_default_workspace()
    ws = get_default_workspace_id()
    assert ws is not None
    return ws


def _sendable_lead(ws: int, email: str, *, body: str = "Hi there, real body.") -> int:
    with session_scope() as s:
        lead = Lead(
            first_name="T", last_name="U", email=email, workspace_id=ws,
            email_verification_status="verified",
            email_verification_provider="osp_lead_engine",
            email_verified_at=now_utc(),
        )
        s.add(lead)
        s.flush()
        lid = lead.id
        s.add(Score(lead_id=lid, score=95, tier="A", rationale="x",
                    signals_used=[], model="t", workspace_id=ws))
        s.add(GeneratedContent(lead_id=lid, kind="email", subject="hi", body=body,
                               signals_cited=[], prompt_version="v", model="m", workspace_id=ws))
        return lid


class _Delivered:
    delivered = True
    skip_reason = None
    delivery_id = "inst-1"


class _Blocked:
    delivered = False
    skip_reason = "unsafe_internal_review_content"
    delivery_id = None


def _spy_deliver(record, *, raises_for=None, blocks_for=None):
    async def _deliver(lid, *, dry_run, workspace_id=None):
        record.append(lid)
        if raises_for and lid in raises_for:
            raise RuntimeError("Instantly API 500")
        if blocks_for and lid in blocks_for:
            return _Blocked()
        return _Delivered()
    return _deliver


# ---------------------------------------------------------------------------
# 1 & 7 & 8 & 9. Push calls deliver for selected eligible leads only
# ---------------------------------------------------------------------------

def test_pushes_selected_eligible_only():
    ws = _seed()
    a = _sendable_lead(ws, "a@x.com")
    b = _sendable_lead(ws, "b@x.com")
    unselected = _sendable_lead(ws, "c@x.com")  # eligible but NOT selected
    called: list[int] = []

    rec = asyncio.run(run_bulk_push([a, b], ws, campaign_id=CAMP, deliver=_spy_deliver(called)))

    assert rec["status"] == "completed"
    assert rec["n_sent"] == 2
    assert rec["n_failed"] == 0
    assert set(called) == {a, b}
    assert unselected not in called  # unselected never pushed


# ---------------------------------------------------------------------------
# 4. Blocked leads → clear blocked reason, deliver not called for them
# ---------------------------------------------------------------------------

def test_blocked_no_campaign_configured():
    ws = _seed()
    a = _sendable_lead(ws, "a@x.com")
    called: list[int] = []
    rec = asyncio.run(run_bulk_push([a], ws, campaign_id="", deliver=_spy_deliver(called)))
    assert rec["status"] == "blocked"
    assert "campaign" in rec["error"].lower()
    assert called == []  # never attempted


def test_blocked_when_no_eligible_leads():
    ws = _seed()
    # Lead with no content → ineligible (no_content).
    with session_scope() as s:
        lead = Lead(first_name="T", last_name="U", email="n@x.com", workspace_id=ws,
                    email_verification_status="verified",
                    email_verification_provider="x", email_verified_at=now_utc())
        s.add(lead)
        s.flush()
        lid = lead.id
        s.add(Score(lead_id=lid, score=95, tier="A", rationale="x",
                    signals_used=[], model="t", workspace_id=ws))
    called: list[int] = []
    rec = asyncio.run(run_bulk_push([lid], ws, campaign_id=CAMP, deliver=_spy_deliver(called)))
    assert rec["status"] == "blocked"
    assert called == []
    assert rec["blocked_reasons"].get("no_content") == 1


# ---------------------------------------------------------------------------
# 5. API failure → clear error, recorded as failure, not sent
# ---------------------------------------------------------------------------

def test_api_failure_recorded_not_sent():
    ws = _seed()
    a = _sendable_lead(ws, "a@x.com")
    called: list[int] = []
    rec = asyncio.run(run_bulk_push(
        [a], ws, campaign_id=CAMP, deliver=_spy_deliver(called, raises_for={a})
    ))
    assert rec["status"] == "completed"
    assert rec["n_sent"] == 0
    assert rec["n_failed"] == 1
    assert rec["failures"] and "Instantly API 500" in rec["failures"][0][1]


# ---------------------------------------------------------------------------
# 6. Blocked deliver result → not counted as sent
# ---------------------------------------------------------------------------

def test_blocked_delivery_not_marked_sent():
    ws = _seed()
    a = _sendable_lead(ws, "a@x.com")
    called: list[int] = []
    rec = asyncio.run(run_bulk_push(
        [a], ws, campaign_id=CAMP, deliver=_spy_deliver(called, blocks_for={a})
    ))
    assert rec["n_sent"] == 0
    assert rec["n_failed"] == 1
    assert "blocked" in rec["failures"][0][1]


# ---------------------------------------------------------------------------
# 10 & 11. Unsafe / missing content excluded by the server-side filter
# ---------------------------------------------------------------------------

def test_unsafe_and_missing_content_blocked_server_side():
    ws = _seed()
    good = _sendable_lead(ws, "good@x.com")
    unsafe = _sendable_lead(ws, "unsafe@x.com",
                            body="NEEDS REVIEW: No direct buyer account found.")
    empty = _sendable_lead(ws, "empty@x.com", body="   ")
    called: list[int] = []

    rec = asyncio.run(run_bulk_push(
        [good, unsafe, empty], ws, campaign_id=CAMP, deliver=_spy_deliver(called)
    ))
    # Only the good lead was delivered; unsafe/empty filtered out server-side.
    assert called == [good]
    assert rec["n_sent"] == 1
    assert rec["blocked_count"] == 2
    assert rec["blocked_reasons"].get("unsafe_content") == 1
    assert rec["blocked_reasons"].get("no_content") == 1


# ---------------------------------------------------------------------------
# 12. Workspace isolation — only the given (workspace-scoped) ids are touched
# ---------------------------------------------------------------------------

def test_workspace_isolation_other_ws_lead_not_pushed():
    ws = _seed()
    other = create_workspace(name="WS B", slug="ws-b", instantly_campaign_id="c-b")["id"]
    a = _sendable_lead(ws, "a@x.com")
    other_lead = _sendable_lead(other, "b@x.com")
    called: list[int] = []

    rec = asyncio.run(run_bulk_push([a], ws, campaign_id=CAMP, deliver=_spy_deliver(called)))
    assert called == [a]
    assert other_lead not in called


# ---------------------------------------------------------------------------
# 2 & 3. Result record shape (persistable across rerun) + no premature reset
# ---------------------------------------------------------------------------

def test_result_record_is_persistable():
    ws = _seed()
    a = _sendable_lead(ws, "a@x.com")
    rec = asyncio.run(run_bulk_push([a], ws, campaign_id=CAMP, deliver=_spy_deliver([])))
    # The record is a plain dict the page stores in session_state — survives rerun.
    for key in ("status", "n_sent", "n_failed", "blocked_count", "blocked_reasons",
                "failures", "started_at", "completed_at", "selected_count"):
        assert key in rec
    assert rec["completed_at"] is not None  # push finished before returning
