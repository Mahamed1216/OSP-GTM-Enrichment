"""H4/H5 webhook security hardening tests (src/webhook/server.py).

Covers:
  - H4: constant-time secret comparison (behavioral correctness of the helper;
        valid still works, wrong/missing rejected).
  - H5: body-size limit returns 413 before processing; per-IP rate limiting
        returns 429 after the configured limit.

Rate-limited / oversized requests are rejected by middleware BEFORE the secret
check, body parse, Anthropic calls, or DB writes — so these tests need no
network/LLM mocking.
"""
from __future__ import annotations

import os

# Must be set before importing src.webhook.server so _expected_secret() works.
os.environ["INSTANTLY_WEBHOOK_SECRET"] = "test-webhook-secret-xyz"

import pytest
from fastapi.testclient import TestClient

from src.webhook.server import app, _secret_matches, reset_rate_limit_state

TEST_SECRET = "test-webhook-secret-xyz"
VALID_HEADERS = {"X-Webhook-Secret": TEST_SECRET}
_BASE_PAYLOAD = {"event": "lead_replied", "email": "x@example.com", "reply_body": "hi"}

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_state():
    """Isolate rate-limit state between tests."""
    reset_rate_limit_state()
    yield
    reset_rate_limit_state()


# ---------------------------------------------------------------------------
# H4 — constant-time secret comparison
# ---------------------------------------------------------------------------

def test_secret_matches_helper_behavior():
    assert _secret_matches("abc", "abc") is True
    assert _secret_matches("abc", "abd") is False
    assert _secret_matches("", "abc") is False        # empty provided
    assert _secret_matches(None, "abc") is False       # missing provided
    assert _secret_matches("abc", "") is False         # unset expected — never authenticate


def test_missing_secret_returns_401_without_processing():
    resp = client.post("/api/instantly/reply-webhook", json=_BASE_PAYLOAD)
    assert resp.status_code == 401


def test_wrong_secret_returns_401_without_processing():
    resp = client.post(
        "/api/instantly/reply-webhook",
        json=_BASE_PAYLOAD,
        headers={"X-Webhook-Secret": "wrong"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# H5 — body-size limit (413)
# ---------------------------------------------------------------------------

def test_oversized_body_returns_413_before_processing(monkeypatch):
    # Tiny cap so a normal payload exceeds it; reset so 429 can't mask the 413.
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", "5")
    reset_rate_limit_state()
    resp = client.post(
        "/api/instantly/reply-webhook", json=_BASE_PAYLOAD, headers=VALID_HEADERS
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def _chunked_body():
    """A bytes generator makes httpx use chunked transfer-encoding, which omits
    the Content-Length header — simulating a client that drops it."""
    yield b'{"event":"lead_replied","email":"x@example.com","reply_body":"hi"}'


def test_missing_content_length_reply_webhook_returns_411(monkeypatch):
    reset_rate_limit_state()
    resp = client.post(
        "/api/instantly/reply-webhook",
        content=_chunked_body(),
        headers={"X-Webhook-Secret": TEST_SECRET, "Content-Type": "application/json"},
    )
    assert resp.status_code == 411
    assert "content-length" in resp.json()["detail"].lower()


def test_missing_content_length_scheduler_returns_411(monkeypatch):
    monkeypatch.setenv("LEAD_SOURCE_JOB_SECRET", "job-secret-xyz")
    reset_rate_limit_state()
    resp = client.post(
        "/api/lead-source/run-scheduled",
        content=_chunked_body(),
        headers={"X-Job-Secret": "job-secret-xyz", "Content-Type": "application/json"},
    )
    assert resp.status_code == 411


def test_normal_body_under_limit_not_413(monkeypatch):
    # Generous cap — body-size guard must not trip for a normal payload.
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", "1048576")
    reset_rate_limit_state()
    resp = client.post(
        "/api/instantly/reply-webhook",
        json=_BASE_PAYLOAD,
        headers={"X-Webhook-Secret": "wrong"},  # wrong secret → 401, but NOT 413
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# H5 — rate limiting (429)
# ---------------------------------------------------------------------------

def test_rate_limit_triggers_after_configured_requests(monkeypatch):
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT", "3")
    monkeypatch.setenv("WEBHOOK_RATE_WINDOW", "60")
    reset_rate_limit_state()

    # Use a wrong secret: middleware rate-limit runs BEFORE the secret check,
    # so the first 3 reach the (failing) auth → 401, the 4th is rate-limited.
    bad = {"X-Webhook-Secret": "wrong"}
    codes = [
        client.post("/api/instantly/reply-webhook", json=_BASE_PAYLOAD, headers=bad).status_code
        for _ in range(4)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429


def test_rate_limit_also_applies_to_scheduler_endpoint(monkeypatch):
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT", "2")
    monkeypatch.setenv("WEBHOOK_RATE_WINDOW", "60")
    # Configure the job secret so a wrong header yields 401 (not the
    # 500 "not configured" path), isolating the rate-limit behavior.
    monkeypatch.setenv("LEAD_SOURCE_JOB_SECRET", "job-secret-xyz")
    reset_rate_limit_state()

    bad = {"X-Job-Secret": "wrong"}
    codes = [
        client.post("/api/lead-source/run-scheduled", json={}, headers=bad).status_code
        for _ in range(3)
    ]
    assert codes[:2] == [401, 401]
    assert codes[2] == 429


def test_health_endpoint_not_rate_limited(monkeypatch):
    # GET /health is not a POST → exempt from the abuse guards.
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT", "1")
    reset_rate_limit_state()
    for _ in range(5):
        assert client.get("/health").status_code == 200
