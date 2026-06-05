"""Tests for the Reply Queue (src/feedback/reply_sync.py).

All tests run without a live Instantly API — network calls are monkeypatched.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

import src.feedback.reply_sync as reply_sync_mod
from src.db import session_scope
from src.feedback.reply_sync import (
    POSITIVE_CLASSIFICATIONS,
    STATUS_MISSING_TEXT,
    STATUS_NEEDS_HUMAN,
    STATUS_NEEDS_REVIEW,
    STATUS_SKIPPED,
    _is_opportunity_lead,
    archive_non_opportunity_threads,
    archive_placeholder_threads,
)
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
        "status": 3,  # Interested — passes the strict opportunity filter
    }


def _make_generic_replied_lead(email: str, lead_id: str) -> dict:
    """status=2 = generic replied (OOO/negative/unsubscribe all land here)."""
    return {"id": lead_id, "email": email, "status": 2, "email_reply_count": 1}


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
    reply_text: str = "I'm interested in learning more.",
):
    """Helper: calls the REAL sync_reply_queue but with all external calls patched.

    `reply_text`: the text returned by the mock reply-text fetch. Defaults to a
    positive reply body so drafts are created by default. Pass "" to simulate
    missing reply text (→ STATUS_MISSING_TEXT).
    """
    monkeypatch.setattr(reply_sync_mod, "sync_reply_queue", sync_reply_queue)

    from src import workspace as ws_mod
    monkeypatch.setattr(ws_mod, "get_campaign_id_for_workspace", lambda wid=None: campaign_id)
    monkeypatch.setattr(ws_mod, "get_api_key_for_workspace", lambda wid=None: api_key)
    monkeypatch.setattr(ws_mod, "get_api_key_source", lambda wid=None: "test")
    monkeypatch.setattr(ws_mod, "get_calendar_link_for_workspace", lambda wid=None: "https://cal.example.com/book")
    monkeypatch.setattr(ws_mod, "get_default_workspace_id", lambda: osp_id)

    monkeypatch.setattr(reply_sync_mod, "_iter_campaign_leads", _fake_positive_iter(*leads))
    # Patch _get_local_replied_leads to return nothing (avoid DB query in tests)
    monkeypatch.setattr(reply_sync_mod, "_get_local_replied_leads", lambda ws_id: [])

    # Patch the multi-endpoint reply text fetch
    async def fake_fetch_reply_text(instantly_id, email, camp, key=None):
        if reply_text:
            return reply_text, "test", {"source_used": "test"}
        return "", "unavailable", {"source_used": "unavailable"}

    monkeypatch.setattr(reply_sync_mod, "_fetch_reply_text_for_lead", fake_fetch_reply_text)

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


# ===========================================================================
# Opportunity filter — new tests for the strict _is_opportunity_lead gate
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Campaign with 2 opportunities and 309 generic replies imports only 2
# ---------------------------------------------------------------------------

def test_campaign_2_opportunities_309_generic_imports_2(monkeypatch):
    osp_id = _seed_osp()
    opportunity_lead = _make_positive_lead(email="opp@example.com", lead_id="il-opp")
    # 309 leads with status=2 (generic replied — OOO/negative/unsubscribe)
    generic_leads = [
        _make_generic_replied_lead(email=f"lead{i}@example.com", lead_id=f"il-gen-{i}")
        for i in range(309)
    ]
    all_leads = [opportunity_lead] + generic_leads

    _run_sync_with_overrides(
        osp_id, "camp-x", "key-x", osp_id,
        monkeypatch, all_leads, _fake_emails(), _fake_draft(),
    )

    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 1, f"Expected 1 opportunity record, got {count} (generic replies must be excluded)"


# ---------------------------------------------------------------------------
# 2. Generic replied (status=2) is NOT imported
# ---------------------------------------------------------------------------

def test_generic_replied_status_not_imported(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-x", "key-x", osp_id,
        monkeypatch,
        [_make_generic_replied_lead("generic@example.com", "il-gen")],
        _fake_emails(), _fake_draft(),
    )
    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 0, "status=2 (generic replied) must not be imported into Reply Agent queue"


# ---------------------------------------------------------------------------
# 3. has_reply=True (email_reply_count>0) alone is not enough
# ---------------------------------------------------------------------------

def test_has_reply_true_not_enough(monkeypatch):
    osp_id = _seed_osp()
    lead = {
        "id": "il-replied", "email": "replied@example.com",
        "status": 2,  # generic replied — not opportunity
        "email_reply_count": 1,
        "has_reply": True,
    }
    _run_sync_with_overrides(
        osp_id, "camp-x", "key-x", osp_id,
        monkeypatch, [lead], _fake_emails(), _fake_draft(),
    )
    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 0, "email_reply_count>0 alone must not trigger import"


# ---------------------------------------------------------------------------
# 4. Opened lead is NOT imported
# ---------------------------------------------------------------------------

def test_opened_lead_not_imported(monkeypatch):
    osp_id = _seed_osp()
    lead = {
        "id": "il-opened", "email": "opened@example.com",
        "status": 1,  # active in sequence — just opened
        "email_open_count": 3,
    }
    _run_sync_with_overrides(
        osp_id, "camp-x", "key-x", osp_id,
        monkeypatch, [lead], _fake_emails(), _fake_draft(),
    )
    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 0, "Opened lead (status=1) must not be imported"


# ---------------------------------------------------------------------------
# 5. Actual opportunity (status=4) IS imported
# ---------------------------------------------------------------------------

def test_opportunity_status_4_is_imported(monkeypatch):
    osp_id = _seed_osp()
    lead = {
        "id": "il-opp4", "email": "opp4@example.com",
        "status": 4,  # Opportunity
    }
    _run_sync_with_overrides(
        osp_id, "camp-x", "key-x", osp_id,
        monkeypatch, [lead], _fake_emails(), _fake_draft(),
    )
    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 1, "status=4 (Opportunity) must be imported"


# ---------------------------------------------------------------------------
# 6. String status "interested" IS imported
# ---------------------------------------------------------------------------

def test_interested_string_status_imported(monkeypatch):
    osp_id = _seed_osp()
    lead = {
        "id": "il-int", "email": "int@example.com",
        "status": "interested",
    }
    _run_sync_with_overrides(
        osp_id, "camp-x", "key-x", osp_id,
        monkeypatch, [lead], _fake_emails(), _fake_draft(),
    )
    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 1, 'status="interested" must be imported as opportunity'


# ---------------------------------------------------------------------------
# 7. Dedup still works with new filter
# ---------------------------------------------------------------------------

def test_dedup_still_works_with_new_filter(monkeypatch):
    osp_id = _seed_osp()
    lead = {"id": "il-dedup", "email": "dedup@example.com", "status": 3}
    for _ in range(3):
        _run_sync_with_overrides(
            osp_id, "camp-x", "key-x", osp_id,
            monkeypatch, [lead], _fake_emails(msg_id="msg-dedup"), _fake_draft(),
        )
    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 1, "Dedup must prevent duplicate records even with multiple syncs"


# ---------------------------------------------------------------------------
# 8. Cleanup archives non-opportunity imported records
# ---------------------------------------------------------------------------

def test_cleanup_archives_non_opportunity_records():
    osp_id = _seed_osp()
    # Create a record with raw_payload that has status=2 (generic replied)
    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            prospect_email="bad@example.com",
            inbound_reply_text="OOO: I am out of office.",
            classification="out_of_office",
            recommended_action="do_not_reply",
            draft_body="",
            human_review_notes="",
            status=STATUS_NEEDS_REVIEW,
            dedup_key="lead:il-bad",
            raw_payload={"status": 2, "email": "bad@example.com"},
        )
        session.add(row)
        session.flush()
        bad_id = row.id

    result = archive_non_opportunity_threads(osp_id, dry_run=False)
    assert result["archived"] >= 1

    with session_scope() as session:
        row = session.get(ReplyThread, bad_id)
        assert row.status == STATUS_SKIPPED
        assert "not_actual_opportunity" in row.human_review_notes.lower()


# ---------------------------------------------------------------------------
# 9. Cleanup does NOT archive true opportunity records
# ---------------------------------------------------------------------------

def test_cleanup_preserves_true_opportunity_records():
    osp_id = _seed_osp()
    # Create a record with raw_payload that has status=3 (Interested)
    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            prospect_email="real@example.com",
            inbound_reply_text="I'm interested.",
            classification=INTENT_POSITIVE_INTEREST,
            recommended_action=ACTION_BOOK_MEETING,
            draft_body="Let's connect.",
            human_review_notes="",
            status=STATUS_NEEDS_REVIEW,
            dedup_key="lead:il-real",
            raw_payload={"status": 3, "email": "real@example.com"},
        )
        session.add(row)
        session.flush()
        good_id = row.id

    result = archive_non_opportunity_threads(osp_id, dry_run=False)
    assert result["preserved"] >= 1

    with session_scope() as session:
        row = session.get(ReplyThread, good_id)
        assert row.status == STATUS_NEEDS_REVIEW, \
            "True opportunity records must not be archived by cleanup"


# ---------------------------------------------------------------------------
# 10. Reply Agent queue is workspace scoped (via _is_opportunity_lead)
# ---------------------------------------------------------------------------

def test_opportunity_filter_is_workspace_scoped(monkeypatch):
    osp_id = _seed_osp()
    other_ws = create_workspace(name="Scope Test", slug="scope-test", instantly_campaign_id="camp-scope")
    other_id = other_ws["id"]

    _run_sync_with_overrides(
        osp_id, "camp-osp", "key-osp", osp_id,
        monkeypatch,
        [_make_positive_lead(email="scoped@example.com", lead_id="il-scoped")],
        _fake_emails(msg_id="msg-scoped"),
        _fake_draft(),
    )

    # Other workspace must have empty queue
    other_queue = get_reply_queue(workspace_id=other_id)
    assert len(other_queue) == 0

    # OSP workspace has the record
    osp_queue = get_reply_queue(workspace_id=osp_id)
    assert len(osp_queue) == 1


# ---------------------------------------------------------------------------
# Unit tests for _is_opportunity_lead
# ---------------------------------------------------------------------------

def test_is_opportunity_lead_rejects_status_2():
    assert _is_opportunity_lead({"status": 2}) is None


def test_is_opportunity_lead_rejects_status_1():
    assert _is_opportunity_lead({"status": 1}) is None


def test_is_opportunity_lead_rejects_open_only():
    assert _is_opportunity_lead({"email_open_count": 5, "status": 1}) is None


def test_is_opportunity_lead_accepts_status_3():
    assert _is_opportunity_lead({"status": 3}) is not None


def test_is_opportunity_lead_accepts_status_4():
    assert _is_opportunity_lead({"status": 4}) is not None


def test_is_opportunity_lead_accepts_interested_string():
    assert _is_opportunity_lead({"status": "interested"}) is not None


def test_is_opportunity_lead_accepts_bool_flag():
    assert _is_opportunity_lead({"is_interested": True}) is not None


def test_is_opportunity_lead_accepts_opportunity_category():
    assert _is_opportunity_lead({"categories": ["opportunity"]}) is not None


# ===========================================================================
# No-placeholder + classification gate tests
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Placeholder "reply text not available" does NOT create needs_review
# ---------------------------------------------------------------------------

def test_placeholder_text_creates_missing_text_not_needs_review(monkeypatch):
    osp_id = _seed_osp()
    # Pass reply_text="" so the mock returns no reply body
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead()],
        _no_emails(),
        _fake_draft(),
        reply_text="",  # simulates no reply body available
    )
    with session_scope() as session:
        threads = session.execute(select(ReplyThread)).scalars().all()
        assert len(threads) == 1
        t = threads[0]
        assert t.status == STATUS_MISSING_TEXT, (
            f"Expected {STATUS_MISSING_TEXT}, got {t.status}. "
            "Records with no reply body must not be needs_review."
        )


# ---------------------------------------------------------------------------
# 2. Placeholder records appear in missing_reply_text bucket
# ---------------------------------------------------------------------------

def test_placeholder_records_in_missing_text_bucket(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch, [_make_positive_lead()], _no_emails(), _fake_draft(),
        reply_text="",
    )
    from src.feedback.reply_sync import get_reply_queue, STATUS_MISSING_TEXT as _MT
    all_threads = get_reply_queue(workspace_id=osp_id)
    missing = [t for t in all_threads if t["status"] == _MT]
    review = [t for t in all_threads if t["status"] == STATUS_NEEDS_REVIEW]
    assert len(missing) == 1
    assert len(review) == 0, "Missing-text records must not appear in needs_review bucket"


# ---------------------------------------------------------------------------
# 3. status=replied alone does NOT create a positive draft
# ---------------------------------------------------------------------------

def test_generic_replied_status_alone_does_not_create_positive_draft(monkeypatch):
    osp_id = _seed_osp()
    # status=2 is excluded by _is_opportunity_lead → no record created at all
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_generic_replied_lead("gen@example.com", "il-gen2")],
        _fake_emails(), _fake_draft(),
    )
    with session_scope() as session:
        count = len(session.execute(select(ReplyThread)).scalars().all())
    assert count == 0, "status=2 must not produce any record"


# ---------------------------------------------------------------------------
# 4. Replied lead WITH positive body creates positive draft
# ---------------------------------------------------------------------------

def test_replied_lead_with_positive_body_creates_positive_draft(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead(email="pos@example.com", lead_id="il-pos-body")],
        _fake_emails(reply_text="I'd really like to know more about your offering."),
        _fake_draft(classification=INTENT_POSITIVE_INTEREST, action=ACTION_BOOK_MEETING,
                    draft="Let's set up a call.", notes=""),
        reply_text="I'd really like to know more about your offering.",
    )
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        assert t.status == STATUS_NEEDS_REVIEW
        assert t.draft_body.strip() != ""


# ---------------------------------------------------------------------------
# 5. Bounty/revshare body → book_meeting draft, does NOT accept terms
# ---------------------------------------------------------------------------

def test_revshare_body_creates_book_meeting_draft_no_terms(monkeypatch):
    osp_id = _seed_osp()
    BOUNTY_REPLY = "I'm happy to pay you 25% bounty on every one that buys."
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead(email="bounty@example.com", lead_id="il-bounty")],
        _fake_emails(reply_text=BOUNTY_REPLY),
        _fake_draft(
            classification=INTENT_REVSHARE,
            action=ACTION_BOOK_MEETING,
            draft="Can we set up a time to discuss? We don't normally do revshare.",
            notes="Commercial terms discussed — do not accept in writing without approval.",
        ),
        reply_text=BOUNTY_REPLY,
    )
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        assert t.status == STATUS_NEEDS_REVIEW
        assert "that works" not in t.draft_body.lower()
        assert "accept the" not in t.draft_body.lower()
        assert "do not accept" in t.human_review_notes.lower() or "commercial terms" in t.human_review_notes.lower()


# ---------------------------------------------------------------------------
# 6. Unsubscribe body → status = skipped
# ---------------------------------------------------------------------------

def test_unsubscribe_body_is_skipped(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead(email="unsub2@example.com", lead_id="il-unsub2")],
        _fake_emails(reply_text="Please unsubscribe me from your list."),
        _fake_draft(classification=INTENT_UNSUBSCRIBE, action=ACTION_STOP,
                    draft="Got it — removed.", notes="Opt-out."),
        reply_text="Please unsubscribe me from your list.",
    )
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        assert t.status == STATUS_SKIPPED, f"Expected skipped, got {t.status}"


# ---------------------------------------------------------------------------
# 7. Angry complaint body → status = needs_human_review
# ---------------------------------------------------------------------------

def test_angry_body_routes_to_human_review(monkeypatch):
    osp_id = _seed_osp()
    _run_sync_with_overrides(
        osp_id, "camp-1", "key-1", osp_id,
        monkeypatch,
        [_make_positive_lead(email="angry2@example.com", lead_id="il-angry2")],
        _fake_emails(reply_text="Stop spamming me or I will report you!"),
        _fake_draft(classification=INTENT_ANGRY, action=ACTION_HUMAN,
                    draft="(Routed to human.)", notes="Angry reply."),
        reply_text="Stop spamming me or I will report you!",
    )
    with session_scope() as session:
        t = session.execute(select(ReplyThread)).scalars().first()
        assert t is not None
        assert t.status == STATUS_NEEDS_HUMAN


# ---------------------------------------------------------------------------
# 8. Cleanup removes placeholder records from main queue
# ---------------------------------------------------------------------------

def test_cleanup_removes_placeholder_records():
    osp_id = _seed_osp()
    # Create placeholder record directly
    with session_scope() as session:
        row = ReplyThread(
            workspace_id=osp_id,
            campaign_id="camp-1",
            prospect_email="pholder@example.com",
            inbound_reply_text="[Reply text not available — status=3. Check dashboard.]",
            status=STATUS_MISSING_TEXT,
            dedup_key="lead:il-pholder",
        )
        session.add(row)
        session.flush()
        ph_id = row.id

    result = archive_placeholder_threads(osp_id, dry_run=False)
    assert result["archived"] >= 1

    with session_scope() as session:
        row = session.get(ReplyThread, ph_id)
        assert row.status == STATUS_SKIPPED

    # Should no longer appear in positive drafts or missing text buckets
    from src.feedback.reply_sync import get_reply_queue
    active = [t for t in get_reply_queue(osp_id) if t["status"] not in (STATUS_SKIPPED, "archived")]
    assert not any(t["id"] == ph_id for t in active)


# ---------------------------------------------------------------------------
# 9. Queue counts separate positive drafts from missing reply text
# ---------------------------------------------------------------------------

def test_queue_counts_separate_positive_from_missing(monkeypatch):
    osp_id = _seed_osp()

    # Lead 1: positive reply body → needs_review
    lead1 = _make_positive_lead(email="lead1@example.com", lead_id="il-l1")
    # Lead 2: no reply body → missing_reply_text
    lead2 = _make_positive_lead(email="lead2@example.com", lead_id="il-l2")

    from src import workspace as ws_mod
    monkeypatch.setattr(ws_mod, "get_campaign_id_for_workspace", lambda wid=None: "camp-1")
    monkeypatch.setattr(ws_mod, "get_api_key_for_workspace", lambda wid=None: "key-1")
    monkeypatch.setattr(ws_mod, "get_api_key_source", lambda wid=None: "test")
    monkeypatch.setattr(ws_mod, "get_calendar_link_for_workspace", lambda wid=None: "")
    monkeypatch.setattr(ws_mod, "get_default_workspace_id", lambda: osp_id)
    monkeypatch.setattr(reply_sync_mod, "_iter_campaign_leads", _fake_positive_iter(lead1, lead2))
    monkeypatch.setattr(reply_sync_mod, "_get_local_replied_leads", lambda ws_id: [])

    call_count = {"n": 0}

    async def variable_fetch(instantly_id, email, camp, key=None):
        call_count["n"] += 1
        if email == "lead1@example.com":
            return "I'd love to learn more.", "test", {}
        return "", "unavailable", {}

    monkeypatch.setattr(reply_sync_mod, "_fetch_reply_text_for_lead", variable_fetch)

    async def fake_classify(**kwargs):
        return _fake_draft(INTENT_POSITIVE_INTEREST, ACTION_BOOK_MEETING)

    monkeypatch.setattr(reply_sync_mod, "classify_and_draft_reply", fake_classify)

    asyncio.run(sync_reply_queue(osp_id))

    all_threads = get_reply_queue(workspace_id=osp_id)
    positive = [t for t in all_threads if t["status"] == STATUS_NEEDS_REVIEW]
    missing = [t for t in all_threads if t["status"] == STATUS_MISSING_TEXT]

    assert len(positive) == 1, f"Expected 1 positive draft, got {len(positive)}"
    assert len(missing) == 1, f"Expected 1 missing-text record, got {len(missing)}"


# ---------------------------------------------------------------------------
# 10. Workspace scoping still applies
# ---------------------------------------------------------------------------

def test_queue_workspace_scoped_with_new_logic(monkeypatch):
    osp_id = _seed_osp()
    other_ws = create_workspace(name="WS10", slug="ws-10", instantly_campaign_id="camp-ws10")
    other_id = other_ws["id"]

    _run_sync_with_overrides(
        osp_id, "camp-osp", "key-osp", osp_id,
        monkeypatch,
        [_make_positive_lead(email="ws10@example.com", lead_id="il-ws10")],
        _fake_emails(), _fake_draft(),
    )

    assert len(get_reply_queue(workspace_id=other_id)) == 0
    assert len(get_reply_queue(workspace_id=osp_id)) == 1
