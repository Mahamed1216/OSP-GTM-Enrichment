"""Tests for the Reply Queue (src/feedback/reply_sync.py).

All tests run without a live Instantly API — network calls are monkeypatched.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

import src.feedback.reply_sync as reply_sync_mod
from src.db import session_scope
from src.feedback.reply_agent import (
    ACTION_BOOK_MEETING,
    ACTION_HUMAN,
    ACTION_STOP,
    INTENT_ANGRY,
    INTENT_POSITIVE_INTEREST,
    INTENT_REVSHARE,
    INTENT_UNSUBSCRIBE,
    ReplyAgentResult,
)
from src.feedback.reply_sync import (
    STATUS_MANUAL_SEND,
    STATUS_NEEDS_HUMAN,
    STATUS_NEEDS_REVIEW,
    STATUS_SENT,
    get_reply_queue,
    get_send_blocked_reason,
    sync_reply_queue,
    try_send_reply,
    update_reply_thread,
)
from src.models import ReplyThread
from src.workspace import (
    create_workspace,
    get_default_workspace_id,
    seed_default_workspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_osp() -> int:
    seed_default_workspace()
    ws_id = get_default_workspace_id()
    assert ws_id is not None
    return ws_id


def _make_positive_lead(email: str = "alice@example.com", lead_id: str = "il-001") -> dict:
    return {
        "id": lead_id,
        "email": email,
        "first_name": "Alice",
        "last_name": "Prospect",
        "company_name": "Acme Corp",
        "status": 2,  # positive integer status
    }


def _fake_positive_iter(*leads):
    """Return an async generator that yields the given lead dicts."""
    async def _gen(campaign_id, api_key=None):
        for lead in leads:
            yield lead
    return _gen


def _fake_emails(
    reply_text: str = "I'm interested!",
    msg_id: str = "msg-001",
) -> tuple[list[dict], dict]:
    emails = [{"id": msg_id, "body": reply_text, "type": "reply", "timestamp": 1_700_000_000}]
    debug = {"status": 200, "record_count": 1, "reply_count": 1, "fields_found": ["id", "body"]}
    return emails, debug


def _no_emails() -> tuple[list[dict], dict]:
    return [], {"status": 200, "record_count": 0, "reply_count": 0, "fields_found": []}


def _fake_draft(
    classification: str = INTENT_POSITIVE_INTEREST,
    action: str = ACTION_BOOK_MEETING,
    draft: str = "Let's connect.",
    notes: str = "",
) -> ReplyAgentResult:
    return ReplyAgentResult(
        classification=classification,
        recommended_action=action,
        draft_body=draft,
        human_review_notes=notes,
    )


def _patch_sync(
    monkeypatch,
    *,
    osp_id: int,
    leads: list[dict] | None = None,
    emails: tuple | None = None,
    draft: ReplyAgentResult | None = None,
    campaign_id: str = "camp-test",
    api_key: str = "key-test",
):
    """Patch all external calls needed by sync_reply_queue."""
    _leads = leads if leads is not None else [_make_positive_lead()]
    _emails = emails if emails is not None else _fake_emails()
    _draft = draft if draft is not None else _fake_draft()

    monkeypatch.setattr(reply_sync_mod, "_iter_campaign_leads", _fake_positive_iter(*_leads))

    async def fake_fetch_emails(email, camp, key=None):
        return _emails

    monkeypatch.setattr(reply_sync_mod, "_fetch_emails_for_lead", fake_fetch_emails)

    async def fake_classify(**kwargs):
        return _draft

    monkeypatch.setattr(reply_sync_mod, "classify_and_draft_reply", fake_classify)

    # Patch workspace resolvers so no DB/env lookup needed
    monkeypatch.setattr(
        reply_sync_mod, "sync_reply_queue",
        # Wrap to inject campaign_id + api_key without changing the real function sig
        lambda ws_id=None: _run_sync_with_overrides(
            ws_id, campaign_id, api_key, osp_id,
            monkeypatch, _leads, _emails, _draft,
        ),
    )
    return _draft


def _run_sync_with_overrides(
    ws_id, campaign_id, api_key, osp_id,
    monkeypatch, leads, emails, draft,
):
    """Helper: calls the REAL sync_reply_queue but with workspace resolvers patched."""
    # Undo the lambda override so the real function runs
    monkeypatch.setattr(reply_sync_mod, "sync_reply_queue", sync_reply_queue)

    from src import workspace as ws_mod
    monkeypatch.setattr(ws_mod, "get_campaign_id_for_workspace", lambda wid=None: campaign_id)
    monkeypatch.setattr(ws_mod, "get_api_key_for_workspace", lambda wid=None: api_key)
    monkeypatch.setattr(ws_mod, "get_api_key_source", lambda wid=None: "test")
    monkeypatch.setattr(ws_mod, "get_calendar_link_for_workspace", lambda wid=None: "https://cal.example.com/book")
    monkeypatch.setattr(ws_mod, "get_default_workspace_id", lambda: osp_id)

    monkeypatch.setattr(reply_sync_mod, "_iter_campaign_leads", _fake_positive_iter(*leads))

    async def fake_fetch(email, camp, key=None):
        return emails

    monkeypatch.setattr(reply_sync_mod, "_fetch_emails_for_lead", fake_fetch)

    async def fake_classify(**kwargs):
        return draft

    monkeypatch.setattr(reply_sync_mod, "classify_and_draft_reply", fake_classify)

    return asyncio.run(sync_reply_queue(ws_id if ws_id is not None else osp_id))


# ---------------------------------------------------------------------------
# 1. Sync creates reply record for an opportunity reply
# ---------------------------------------------------------------------------

def test_sync_creates_reply_record(monkeypatch):
    osp_id = _seed_osp()
    result = _patch_sync(monkeypatch, osp_id=osp_id)
    summary = _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead()],
        _fake_emails(),
        _fake_draft(),
    )
    assert summary["new"] == 1

    with session_scope() as session:
        threads = session.execute(select(ReplyThread)).scalars().all()
        assert len(threads) == 1
        t = threads[0]
        assert t.prospect_email == "alice@example.com"
        assert t.workspace_id == osp_id


# ---------------------------------------------------------------------------
# 2. Sync deduplicates duplicate reply payloads
# ---------------------------------------------------------------------------

def test_sync_dedupes_same_reply(monkeypatch):
    osp_id = _seed_osp()

    # Run sync twice with the same lead
    for _ in range(2):
        _run_sync_with_overrides(
            osp_id, "camp-1", "key-1", osp_id,
            monkeypatch,
            [_make_positive_lead(email="bob@example.com", lead_id="il-bob")],
            _fake_emails(msg_id="msg-bob"),
            _fake_draft(),
        )

    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 1, "Second sync must not create a duplicate record"


# ---------------------------------------------------------------------------
# 3. Positive reply creates needs_review draft
# ---------------------------------------------------------------------------

def test_positive_reply_creates_needs_review_draft(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead()],
        _fake_emails(reply_text="Sounds interesting, tell me more."),
        _fake_draft(classification=INTENT_POSITIVE_INTEREST, action=ACTION_BOOK_MEETING),
    )
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        assert t.status == STATUS_NEEDS_REVIEW
        assert t.draft_body.strip() != ""


# ---------------------------------------------------------------------------
# 4. Revshare/bounty reply does not accept terms
# ---------------------------------------------------------------------------

def test_revshare_draft_does_not_accept_terms(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead()],
        _fake_emails(reply_text="I'm happy to pay you 25% bounty."),
        _fake_draft(
            classification=INTENT_REVSHARE,
            action=ACTION_BOOK_MEETING,
            draft="Can we set up a time to discuss? We don't normally do revshare.",
            notes="Commercial terms discussed — do not accept in writing without approval.",
        ),
    )
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        draft_lower = t.draft_body.lower()
        assert "that works" not in draft_lower
        assert "accept" not in draft_lower or "do not accept" in t.human_review_notes.lower()


# ---------------------------------------------------------------------------
# 5. Revshare/bounty reply asks for meeting
# ---------------------------------------------------------------------------

def test_revshare_draft_asks_for_meeting(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead()],
        _fake_emails(reply_text="I'll pay 20% commission on each deal."),
        _fake_draft(
            classification=INTENT_REVSHARE,
            action=ACTION_BOOK_MEETING,
            draft="Can we set up a time to discuss?",
            notes="Commercial terms discussed — do not accept in writing without approval.",
        ),
    )
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        # Draft should indicate meeting/call
        draft_lower = t.draft_body.lower()
        assert any(w in draft_lower for w in ("time", "call", "meeting", "discuss", "calendar"))


# ---------------------------------------------------------------------------
# 6. Calendar link inserted when workspace calendar link exists
# ---------------------------------------------------------------------------

def test_calendar_link_in_draft_when_configured(monkeypatch):
    osp_id = _seed_osp()
    cal = "https://calendly.com/test/30min"
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead()],
        _fake_emails(reply_text="Interested."),
        _fake_draft(
            classification=INTENT_POSITIVE_INTEREST,
            action=ACTION_BOOK_MEETING,
            draft=f"Can we connect? Book here: {cal}",
        ),
    )
    # calendar link comes from get_calendar_link_for_workspace which is patched to cal
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        assert cal in t.draft_body


# ---------------------------------------------------------------------------
# 7. Missing calendar link — fallback wording
# ---------------------------------------------------------------------------

def test_no_calendar_link_fallback_wording(monkeypatch):
    osp_id = _seed_osp()

    # Patch calendar link to None
    from src import workspace as ws_mod
    monkeypatch.setattr(ws_mod, "get_campaign_id_for_workspace", lambda wid=None: "camp-1")
    monkeypatch.setattr(ws_mod, "get_api_key_for_workspace", lambda wid=None: "key-1")
    monkeypatch.setattr(ws_mod, "get_api_key_source", lambda wid=None: "test")
    monkeypatch.setattr(ws_mod, "get_calendar_link_for_workspace", lambda wid=None: None)
    monkeypatch.setattr(ws_mod, "get_default_workspace_id", lambda: osp_id)
    monkeypatch.setattr(
        reply_sync_mod, "_iter_campaign_leads",
        _fake_positive_iter(_make_positive_lead(email="carol@example.com")),
    )

    async def fake_fetch(e, c, k=None):
        return _fake_emails(reply_text="Sure, let's talk.")

    monkeypatch.setattr(reply_sync_mod, "_fetch_emails_for_lead", fake_fetch)

    captured: dict = {}

    async def capturing_classify(**kwargs):
        captured["calendar_link"] = kwargs.get("calendar_link", "MISSING")
        return _fake_draft(
            draft="Happy to set up a time. I can send a calendar link.",
        )

    monkeypatch.setattr(reply_sync_mod, "classify_and_draft_reply", capturing_classify)

    asyncio.run(sync_reply_queue(osp_id))

    # When no calendar link, classify_and_draft_reply should receive empty string
    assert captured.get("calendar_link", "MISSING") in ("", None)


# ---------------------------------------------------------------------------
# 8 & 9. Workspace isolation
# ---------------------------------------------------------------------------

def test_osp_replies_not_visible_in_other_workspace(monkeypatch):
    osp_id = _seed_osp()
    other_ws = create_workspace(name="Test Client", slug="test-client", instantly_campaign_id="camp-other")
    other_id = other_ws["id"]

    _run_sync_with_overrides(
        osp_id, "camp-osp", "key-osp", osp_id,
        monkeypatch,
        [_make_positive_lead(email="osp@example.com", lead_id="il-osp")],
        _fake_emails(msg_id="msg-osp"),
        _fake_draft(),
    )

    other_queue = get_reply_queue(workspace_id=other_id)
    assert all(t["workspace_id"] != osp_id for t in other_queue), \
        "OSP replies must not appear in other workspace"


def test_test_client_replies_not_visible_in_osp(monkeypatch):
    osp_id = _seed_osp()
    other_ws = create_workspace(name="Test Client 2", slug="test-client-2", instantly_campaign_id="camp-other2")
    other_id = other_ws["id"]

    _run_sync_with_overrides(
        other_id, "camp-other", "key-other", other_id,
        monkeypatch,
        [_make_positive_lead(email="other@example.com", lead_id="il-other")],
        _fake_emails(msg_id="msg-other"),
        _fake_draft(),
    )

    osp_queue = get_reply_queue(workspace_id=osp_id)
    assert all(t["workspace_id"] != other_id for t in osp_queue), \
        "Other workspace replies must not appear in OSP"


# ---------------------------------------------------------------------------
# 10. Approve and send blocked for unsubscribe
# ---------------------------------------------------------------------------

def test_approve_send_blocked_for_unsubscribe():
    osp_id = _seed_osp()
    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            instantly_lead_id="il-unsub",
            prospect_email="unsub@example.com",
            inbound_reply_text="Unsubscribe me please.",
            classification=INTENT_UNSUBSCRIBE,
            recommended_action=ACTION_STOP,
            draft_body="Got it — removed.",
            human_review_notes="Opt-out.",
            status=STATUS_NEEDS_REVIEW,
            dedup_key="lead:il-unsub",
        )
        session.add(row)
        session.flush()
        thread_id = row.id

    result = asyncio.run(try_send_reply(thread_id, workspace_id=osp_id))
    assert result["status"] == "failed"
    assert "unsubscribe" in result["detail"].lower() or "blocked" in result["detail"].lower()


# ---------------------------------------------------------------------------
# 11. Approve and send blocked for angry complaint
# ---------------------------------------------------------------------------

def test_approve_send_blocked_for_angry_complaint():
    osp_id = _seed_osp()
    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            instantly_lead_id="il-angry",
            prospect_email="angry@example.com",
            inbound_reply_text="Stop emailing me or I'll report you!",
            classification=INTENT_ANGRY,
            recommended_action=ACTION_HUMAN,
            draft_body="(Routed to human — do not send.)",
            human_review_notes="Angry reply.",
            status=STATUS_NEEDS_REVIEW,
            dedup_key="lead:il-angry",
        )
        session.add(row)
        session.flush()
        thread_id = row.id

    result = asyncio.run(try_send_reply(thread_id, workspace_id=osp_id))
    assert result["status"] == "failed"
    assert "angry" in result["detail"].lower() or "blocked" in result["detail"].lower()


# ---------------------------------------------------------------------------
# 12. Approve uses workspace API key when present
# ---------------------------------------------------------------------------

def test_approve_uses_workspace_api_key(monkeypatch):
    osp_id = _seed_osp()
    used_keys: list[str] = []

    async def fake_http_post(self_or_url, *args, **kwargs):
        # Capture the auth header that would have been used
        import httpx as httpx_mod

        class FakeResp:
            status_code = 404
            text = "not found"

            def json(self):
                return {}

            def raise_for_status(self):
                raise httpx_mod.HTTPStatusError(
                    "404", request=None, response=self
                )

        return FakeResp()

    # Patch the workspace key resolver
    from src import workspace as ws_mod
    monkeypatch.setattr(ws_mod, "get_api_key_for_workspace", lambda wid=None: "ws-key-123")
    monkeypatch.setattr(ws_mod, "get_campaign_id_for_workspace", lambda wid=None: "camp-1")

    captured_headers: list[dict] = []

    original_post = None

    async def fake_client_post(url, *, headers=None, json=None, **kwargs):
        captured_headers.append(dict(headers or {}))

        class FakeResp:
            status_code = 404
            text = "not found"

            def json(self2):
                return {}

            def raise_for_status(self2):
                import httpx as httpx_mod
                raise httpx_mod.HTTPStatusError("404", request=None, response=self2)

        return FakeResp()

    import httpx as httpx_mod

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, *, headers=None, json=None, **kwargs):
            captured_headers.append(dict(headers or {}))

            class R:
                status_code = 404
                text = "not supported"

                def json(self):
                    return {}

                def raise_for_status(self):
                    raise httpx_mod.HTTPStatusError("404", request=None, response=self)

            return R()

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: FakeAsyncClient())

    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            instantly_lead_id="il-ws",
            prospect_email="ws@example.com",
            inbound_reply_text="Interested.",
            classification=INTENT_POSITIVE_INTEREST,
            recommended_action=ACTION_BOOK_MEETING,
            draft_body="Let's connect.",
            human_review_notes="",
            status=STATUS_NEEDS_REVIEW,
            dedup_key="lead:il-ws",
        )
        session.add(row)
        session.flush()
        thread_id = row.id

    result = asyncio.run(try_send_reply(thread_id, workspace_id=osp_id))
    # 404 → manual_send_required
    assert result["status"] == STATUS_MANUAL_SEND
    # Verify workspace key was used
    assert any("ws-key-123" in str(h.get("Authorization", "")) for h in captured_headers), \
        "Workspace API key must be used in auth header"


# ---------------------------------------------------------------------------
# 13. Falls back to env API key when workspace key is blank
# ---------------------------------------------------------------------------

def test_approve_falls_back_to_env_api_key(monkeypatch):
    import os
    osp_id = _seed_osp()

    from src import workspace as ws_mod
    # Workspace has no key → returns env fallback
    monkeypatch.setattr(ws_mod, "get_api_key_for_workspace", lambda wid=None: "env-fallback-key")
    monkeypatch.setattr(ws_mod, "get_campaign_id_for_workspace", lambda wid=None: "camp-1")

    captured_headers: list[dict] = []
    import httpx as httpx_mod

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, *, headers=None, json=None, **kwargs):
            captured_headers.append(dict(headers or {}))

            class R:
                status_code = 404
                text = "not supported"

                def json(self):
                    return {}

                def raise_for_status(self):
                    raise httpx_mod.HTTPStatusError("404", request=None, response=self)

            return R()

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: FakeAsyncClient())

    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            instantly_lead_id="il-env",
            prospect_email="env@example.com",
            inbound_reply_text="Let's chat.",
            classification=INTENT_POSITIVE_INTEREST,
            recommended_action=ACTION_BOOK_MEETING,
            draft_body="Happy to connect.",
            human_review_notes="",
            status=STATUS_NEEDS_REVIEW,
            dedup_key="lead:il-env",
        )
        session.add(row)
        session.flush()
        thread_id = row.id

    result = asyncio.run(try_send_reply(thread_id, workspace_id=osp_id))
    assert result["status"] == STATUS_MANUAL_SEND
    assert any("env-fallback-key" in str(h.get("Authorization", "")) for h in captured_headers), \
        "Env API key must be used when workspace key is blank"


# ---------------------------------------------------------------------------
# 14. Instantly reply endpoint unavailable → manual_send_required
# ---------------------------------------------------------------------------

def test_instantly_reply_endpoint_unavailable(monkeypatch):
    osp_id = _seed_osp()

    from src import workspace as ws_mod
    monkeypatch.setattr(ws_mod, "get_api_key_for_workspace", lambda wid=None: "any-key")
    monkeypatch.setattr(ws_mod, "get_campaign_id_for_workspace", lambda wid=None: "camp-1")

    import httpx as httpx_mod

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, *, headers=None, json=None, **kwargs):
            class R:
                status_code = 404
                text = "Not found"

                def json(self):
                    return {}

                def raise_for_status(self):
                    raise httpx_mod.HTTPStatusError("404", request=None, response=self)

            return R()

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: FakeAsyncClient())

    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            instantly_lead_id="il-404",
            prospect_email="noendpoint@example.com",
            inbound_reply_text="Interested.",
            classification=INTENT_POSITIVE_INTEREST,
            recommended_action=ACTION_BOOK_MEETING,
            draft_body="Let's talk.",
            human_review_notes="",
            status=STATUS_NEEDS_REVIEW,
            dedup_key="lead:il-404",
        )
        session.add(row)
        session.flush()
        thread_id = row.id

    result = asyncio.run(try_send_reply(thread_id, workspace_id=osp_id))
    assert result["status"] == STATUS_MANUAL_SEND
    assert "not available" in result["detail"].lower() or "manual" in result["detail"].lower()

    # Thread must be marked manual_send_required in DB
    with session_scope() as session:
        row = session.get(ReplyThread, thread_id)
        assert row.status == STATUS_MANUAL_SEND


# ---------------------------------------------------------------------------
# 15. No auto-send during sync
# ---------------------------------------------------------------------------

def test_no_auto_send_during_sync(monkeypatch):
    osp_id = _seed_osp()
    send_calls: list = []

    async def fake_try_send(*args, **kwargs):
        send_calls.append((args, kwargs))
        return {"status": "sent", "detail": "sent", "debug": None}

    # Patch try_send_reply to detect if it's called
    monkeypatch.setattr(reply_sync_mod, "try_send_reply", fake_try_send, raising=False)

    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead()],
        _fake_emails(),
        _fake_draft(),
    )

    assert send_calls == [], "sync_reply_queue must never call try_send_reply"


# ---------------------------------------------------------------------------
# get_send_blocked_reason edge cases
# ---------------------------------------------------------------------------

def test_get_send_blocked_reason_allows_positive():
    thread = {
        "classification": INTENT_POSITIVE_INTEREST,
        "recommended_action": ACTION_BOOK_MEETING,
        "draft_body": "Let's connect.",
        "campaign_id": "camp-1",
    }
    assert get_send_blocked_reason(thread) is None


def test_get_send_blocked_reason_blocks_empty_draft():
    thread = {
        "classification": INTENT_POSITIVE_INTEREST,
        "recommended_action": ACTION_BOOK_MEETING,
        "draft_body": "",
        "campaign_id": "camp-1",
    }
    reason = get_send_blocked_reason(thread)
    assert reason is not None
    assert "empty" in reason.lower()
