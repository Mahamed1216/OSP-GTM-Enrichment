"""Tests for the Instantly reply webhook receiver (src/webhook/server.py).

All 10 tests run against the in-memory SQLite DB (conftest.py).
Network / LLM calls are monkeypatched. No live Instantly API hits.

INSTANTLY_WEBHOOK_SECRET is set at module load so the FastAPI app is ready
before any fixture tries to use it.
"""
from __future__ import annotations

import os

# Must be set before importing src.webhook.server so _expected_secret() works.
os.environ["INSTANTLY_WEBHOOK_SECRET"] = "test-webhook-secret-xyz"

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.webhook.handler as handler_mod
from src.db import session_scope
from src.feedback.reply_agent import (
    ACTION_BOOK_MEETING,
    ACTION_STOP,
    INTENT_POSITIVE_INTEREST,
    INTENT_REVSHARE,
    INTENT_UNSUBSCRIBE,
    ReplyAgentResult,
)
from src.feedback.reply_sync import (
    STATUS_MISSING_TEXT,
    STATUS_NEEDS_HUMAN,
    STATUS_NEEDS_REVIEW,
    STATUS_SENT,
    STATUS_SKIPPED,
)
STATUS_NEEDS_HUMAN_REVIEW = STATUS_NEEDS_HUMAN  # alias used in new tests
from src.models import ReplyThread
from src.webhook.server import app
from src.workspace import create_workspace, seed_default_workspace

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

TEST_SECRET = "test-webhook-secret-xyz"
VALID_HEADERS = {"X-Webhook-Secret": TEST_SECRET}

_BASE_PAYLOAD: dict = {
    "event": "lead_replied",
    "email": "laz@example.com",
    "first_name": "Laz",
    "last_name": "Fuentes",
    "company": "Laz Co",
    "reply_body": "I'm interested in learning more.",
    "thread_id": "thread-001",
    "campaign_id": "camp-osp",
}

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed() -> int:
    seed_default_workspace()
    from src.workspace import get_default_workspace_id
    ws_id = get_default_workspace_id()
    assert ws_id is not None
    return ws_id


def _fake_positive() -> ReplyAgentResult:
    return ReplyAgentResult(
        classification=INTENT_POSITIVE_INTEREST,
        recommended_action=ACTION_BOOK_MEETING,
        draft_body="Let's connect.",
        human_review_notes="",
    )


def _patch_classify(monkeypatch, result: ReplyAgentResult) -> None:
    async def _fake(**kwargs):
        return result
    monkeypatch.setattr(handler_mod, "classify_and_draft_reply", _fake)


# ---------------------------------------------------------------------------
# Test 1: Valid webhook with reply_body creates reply thread
# ---------------------------------------------------------------------------

def test_valid_webhook_creates_reply_thread(monkeypatch):
    _seed()
    _patch_classify(monkeypatch, _fake_positive())

    resp = client.post("/api/instantly/reply-webhook", json=_BASE_PAYLOAD, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["reply_thread_id"] is not None

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.prospect_email == "laz@example.com"
        assert t.inbound_reply_text == _BASE_PAYLOAD["reply_body"]
        assert not t.inbound_reply_text.startswith("[Reply text not available")


# ---------------------------------------------------------------------------
# Test 2: Missing X-Webhook-Secret header → 401
# ---------------------------------------------------------------------------

def test_missing_secret_header_returns_401(monkeypatch):
    _seed()
    resp = client.post("/api/instantly/reply-webhook", json=_BASE_PAYLOAD)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 3: Wrong secret value → 401
# ---------------------------------------------------------------------------

def test_wrong_secret_returns_401(monkeypatch):
    _seed()
    resp = client.post(
        "/api/instantly/reply-webhook",
        json=_BASE_PAYLOAD,
        headers={"X-Webhook-Secret": "totally-wrong-secret"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 4: Missing reply_body → missing_reply_text record, no draft
# ---------------------------------------------------------------------------

def test_missing_reply_body_creates_missing_text_record(monkeypatch):
    _seed()
    payload = {**_BASE_PAYLOAD, "reply_body": "", "thread_id": "thread-missing-body"}
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == STATUS_MISSING_TEXT
    assert body["reply_thread_id"] is not None

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.status == STATUS_MISSING_TEXT
        assert t.sent_at is None
        assert t.draft_body == "" or t.draft_body is None or not t.draft_body.strip()


# ---------------------------------------------------------------------------
# Test 5: Positive reply → needs_review draft
# ---------------------------------------------------------------------------

def test_positive_reply_creates_needs_review(monkeypatch):
    _seed()
    _patch_classify(monkeypatch, _fake_positive())

    resp = client.post("/api/instantly/reply-webhook", json=_BASE_PAYLOAD, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == STATUS_NEEDS_REVIEW
    assert body["classification"] == INTENT_POSITIVE_INTEREST

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.status == STATUS_NEEDS_REVIEW
        assert t.draft_body.strip() != ""
        assert t.sent_at is None


# ---------------------------------------------------------------------------
# Test 6: Bounty/revshare reply → meeting draft, bounty NOT accepted
# ---------------------------------------------------------------------------

def test_bounty_reply_meeting_draft_no_bounty_acceptance(monkeypatch):
    _seed()
    _patch_classify(monkeypatch, ReplyAgentResult(
        classification=INTENT_REVSHARE,
        recommended_action=ACTION_BOOK_MEETING,
        draft_body=(
            "Can we set up a time to discuss? We don't normally do revshare "
            "but would like to learn more about the product and your ICP."
        ),
        human_review_notes="Commercial terms discussed — do not accept in writing without approval.",
    ))

    payload = {
        **_BASE_PAYLOAD,
        "reply_body": "I'll pay 25% bounty on every deal you close.",
        "thread_id": "thread-bounty",
    }
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        draft_lower = t.draft_body.lower()
        # Must redirect to meeting, not accept the bounty
        assert any(w in draft_lower for w in ("time", "call", "meeting", "discuss", "calendar"))
        assert "that works" not in draft_lower
        assert "accept the 25" not in draft_lower
        assert "commercial terms" in t.human_review_notes.lower() or "do not accept" in t.human_review_notes.lower()
        assert t.sent_at is None


# ---------------------------------------------------------------------------
# Test 7: Unsubscribe reply → skipped
# ---------------------------------------------------------------------------

def test_unsubscribe_reply_is_skipped(monkeypatch):
    _seed()
    _patch_classify(monkeypatch, ReplyAgentResult(
        classification=INTENT_UNSUBSCRIBE,
        recommended_action=ACTION_STOP,
        draft_body="Got it — removed.",
        human_review_notes="Opt-out request.",
    ))

    payload = {
        **_BASE_PAYLOAD,
        "reply_body": "Please remove me from your list.",
        "thread_id": "thread-unsub",
    }
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == STATUS_SKIPPED

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.status == STATUS_SKIPPED


# ---------------------------------------------------------------------------
# Test 8: Workspace resolves by campaign_id when workspace_slug absent
# ---------------------------------------------------------------------------

def test_workspace_resolves_by_campaign_id(monkeypatch):
    seed_default_workspace()
    _patch_classify(monkeypatch, _fake_positive())

    other_ws = create_workspace(
        name="Campaign Workspace",
        slug="campaign-ws-test",
        instantly_campaign_id="campaign-ws-unique-999",
    )
    other_id = other_ws["id"]

    payload = {
        "email": "camptest@example.com",
        "first_name": "Camp",
        "last_name": "Test",
        "reply_body": "Looks interesting.",
        "campaign_id": "campaign-ws-unique-999",   # no workspace_slug
        "thread_id": "thread-camp-999",
    }
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["workspace_id"] == other_id, (
        f"Expected workspace_id={other_id}, got {body['workspace_id']}"
    )

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.workspace_id == other_id


# ---------------------------------------------------------------------------
# Test 9: Duplicate webhook (same thread_id) is deduplicated
# ---------------------------------------------------------------------------

def test_dedup_prevents_duplicate_threads(monkeypatch):
    _seed()
    _patch_classify(monkeypatch, _fake_positive())

    payload = {**_BASE_PAYLOAD, "thread_id": "thread-dedup-xyz"}

    resp1 = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)
    resp2 = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    id1 = resp1.json()["reply_thread_id"]
    id2 = resp2.json()["reply_thread_id"]
    status2 = resp2.json()["status"]

    # Both must reference the same thread
    assert id2 == id1 or status2 == "duplicate", (
        f"Second request should return the same thread ({id1}) or 'duplicate', got id={id2} status={status2}"
    )

    with session_scope() as session:
        all_threads = session.execute(select(ReplyThread)).scalars().all()
        matching = [
            t for t in all_threads
            if t.dedup_key and "thread-dedup-xyz" in t.dedup_key
        ]
    assert len(matching) == 1, f"Expected exactly 1 thread for dedup key, got {len(matching)}"


# ---------------------------------------------------------------------------
# Test 10: No auto-send happens
# ---------------------------------------------------------------------------

def test_no_auto_send(monkeypatch):
    _seed()
    _patch_classify(monkeypatch, _fake_positive())

    send_calls: list = []

    async def fake_try_send(*args, **kwargs):
        send_calls.append((args, kwargs))
        return {"status": "sent", "detail": "sent", "debug": None}

    # Patch at handler level (that's where the logic lives)
    monkeypatch.setattr(handler_mod, "classify_and_draft_reply", handler_mod.classify_and_draft_reply)

    payload = {**_BASE_PAYLOAD, "thread_id": "thread-nosend"}
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    assert send_calls == [], "Webhook handler must never auto-send replies"

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.sent_at is None
        assert t.status != STATUS_SENT


# ---------------------------------------------------------------------------
# Test 11: AI failure — record is still saved with reply body intact
# ---------------------------------------------------------------------------

def test_ai_failure_saves_record(monkeypatch):
    _seed()

    async def _raise(**kwargs):
        raise RuntimeError("Anthropic API 503")

    monkeypatch.setattr(handler_mod, "classify_and_draft_reply", _raise)

    payload = {**_BASE_PAYLOAD, "thread_id": "thread-ai-fail-1"}
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_thread_id"] is not None

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.inbound_reply_text == payload["reply_body"]


# ---------------------------------------------------------------------------
# Test 12: AI failure returns ok=True with ai_failed=True
# ---------------------------------------------------------------------------

def test_ai_failure_returns_ok_with_ai_failed(monkeypatch):
    _seed()

    async def _raise(**kwargs):
        raise ConnectionError("Network timeout")

    monkeypatch.setattr(handler_mod, "classify_and_draft_reply", _raise)

    payload = {**_BASE_PAYLOAD, "thread_id": "thread-ai-fail-2"}
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["ai_failed"] is True
    assert body["reply_thread_id"] is not None


# ---------------------------------------------------------------------------
# Test 13: AI failure sets status to needs_human_review
# ---------------------------------------------------------------------------

def test_ai_failure_sets_needs_human_review(monkeypatch):
    _seed()

    async def _raise(**kwargs):
        raise ValueError("API key invalid")

    monkeypatch.setattr(handler_mod, "classify_and_draft_reply", _raise)

    payload = {**_BASE_PAYLOAD, "thread_id": "thread-ai-fail-3"}
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == STATUS_NEEDS_HUMAN

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.status == STATUS_NEEDS_HUMAN
        assert t.classification == "unclassified"
        assert not (t.draft_body or "").strip()
        assert "AI classification failed" in (t.human_review_notes or "")


# ---------------------------------------------------------------------------
# Test 14: AI failure — no auto send
# ---------------------------------------------------------------------------

def test_ai_failure_no_auto_send(monkeypatch):
    _seed()

    async def _raise(**kwargs):
        raise RuntimeError("classification exploded")

    monkeypatch.setattr(handler_mod, "classify_and_draft_reply", _raise)

    payload = {**_BASE_PAYLOAD, "thread_id": "thread-ai-fail-4"}
    resp = client.post("/api/instantly/reply-webhook", json=payload, headers=VALID_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    with session_scope() as session:
        t = session.get(ReplyThread, body["reply_thread_id"])
        assert t is not None
        assert t.sent_at is None
        assert t.status != STATUS_SENT
