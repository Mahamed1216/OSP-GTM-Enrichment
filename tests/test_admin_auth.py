"""Admin password login for the operator console.

The console signs in with ADMIN_PASSWORD and carries an HttpOnly session
cookie. INTERNAL_API_KEY stays a server-side credential for backend-to-backend
callers and must never be required from a browser.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.api import auth
from src.api.server import app

PASSWORD = "correct-horse-battery-staple"
INTERNAL_KEY = "internal-only-key"

# One protected route from each family the console uses.
PROTECTED = [
    ("GET", "/api/v1/leads"),
    ("GET", "/api/v1/runs"),
    ("GET", "/api/v1/settings"),
    ("GET", "/api/v1/prompts"),
    ("GET", "/api/v1/dashboard/summary"),
]


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL_KEY)
    from src.workspace import seed_default_workspace
    seed_default_workspace()
    return TestClient(app)


def _request(client: TestClient, method: str, path: str, **kwargs):
    return client.request(method, path, json={} if method == "POST" else None, **kwargs)


# ---------------------------------------------------------------------------
# Login / logout / me
# ---------------------------------------------------------------------------

def test_correct_password_logs_in_and_sets_a_cookie(client):
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}

    cookie = response.cookies.get(auth.COOKIE_NAME)
    assert cookie, "login did not set a session cookie"
    # The cookie must not be the password itself.
    assert PASSWORD not in cookie


def test_login_cookie_is_httponly_and_samesite_lax(client):
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    header = response.headers.get("set-cookie", "")
    assert "httponly" in header.lower(), "session cookie must be HttpOnly"
    assert "samesite=lax" in header.lower()


def test_wrong_password_is_rejected(client):
    response = client.post("/api/auth/login", json={"password": "nope"})
    assert response.status_code == 401
    assert response.cookies.get(auth.COOKIE_NAME) is None


def test_empty_password_is_rejected(client):
    assert client.post("/api/auth/login", json={"password": ""}).status_code == 401


def test_missing_admin_password_returns_a_clean_config_error(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    response = TestClient(app).post("/api/auth/login", json={"password": "anything"})
    assert response.status_code == 500
    assert "ADMIN_PASSWORD" in response.json()["detail"]


def test_me_reports_signed_out_then_signed_in(client):
    assert client.get("/api/auth/me").json()["authenticated"] is False
    client.post("/api/auth/login", json={"password": PASSWORD})
    body = client.get("/api/auth/me").json()
    assert body["authenticated"] is True
    assert body["login_configured"] is True


def test_me_never_returns_a_credential(client, monkeypatch):
    client.post("/api/auth/login", json={"password": PASSWORD})
    text = client.get("/api/auth/me").text
    assert PASSWORD not in text
    assert INTERNAL_KEY not in text


def test_logout_clears_access(client):
    client.post("/api/auth/login", json={"password": PASSWORD})
    assert client.get("/api/v1/leads").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["authenticated"] is False
    assert client.get("/api/v1/leads").status_code == 401


# ---------------------------------------------------------------------------
# Protected routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_route_blocked_when_signed_out(client, method, path):
    assert _request(client, method, path).status_code == 401


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_route_works_after_login(client, method, path):
    client.post("/api/auth/login", json={"password": PASSWORD})
    assert _request(client, method, path).status_code == 200


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_internal_key_still_works_for_backend_callers(client, method, path):
    """Cron and scripts keep using the bearer key; the browser never does."""
    response = _request(
        client, method, path, headers={"Authorization": f"Bearer {INTERNAL_KEY}"}
    )
    assert response.status_code == 200


def test_a_forged_cookie_is_rejected(client):
    client.cookies.set(auth.COOKIE_NAME, "not.a.real.token")
    assert client.get("/api/v1/leads").status_code == 401


def test_a_tampered_cookie_is_rejected(client):
    login = client.post("/api/auth/login", json={"password": PASSWORD})
    token = login.cookies.get(auth.COOKIE_NAME)
    payload, _, signature = token.partition(".")
    client.cookies.set(auth.COOKIE_NAME, f"{payload}x.{signature}")
    assert client.get("/api/v1/leads").status_code == 401


# ---------------------------------------------------------------------------
# Token behaviour
# ---------------------------------------------------------------------------

def test_token_round_trips(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    assert auth.verify_token(auth.issue_token()) is True


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    assert auth.verify_token(auth.issue_token(ttl_seconds=-1)) is False


def test_changing_the_password_invalidates_existing_sessions(monkeypatch):
    """The signing key derives from the password when no secret is set."""
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    token = auth.issue_token()
    assert auth.verify_token(token) is True

    monkeypatch.setenv("ADMIN_PASSWORD", "a-different-password")
    assert auth.verify_token(token) is False


def test_explicit_session_secret_survives_a_password_change(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "a-stable-signing-secret")
    token = auth.issue_token()
    monkeypatch.setenv("ADMIN_PASSWORD", "a-different-password")
    assert auth.verify_token(token) is True


def test_verify_is_false_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    assert auth.verify_token("anything.at.all") is False


def test_password_comparison_rejects_empty_configuration(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert auth.password_matches("") is False
    assert auth.password_matches("guess") is False


def test_cookie_is_secure_on_vercel_and_not_locally(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    assert auth.cookie_kwargs()["secure"] is False
    monkeypatch.setenv("VERCEL", "1")
    assert auth.cookie_kwargs()["secure"] is True


def test_token_expiry_is_in_the_future(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    import base64
    import json

    payload = auth.issue_token(ttl_seconds=60).partition(".")[0]
    padding = "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    assert claims["exp"] > int(time.time())


# ---------------------------------------------------------------------------
# The ASGI adapter owns /api/v1/drain, so it needs the same auth
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter(monkeypatch) -> TestClient:
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL_KEY)
    from api.index import app as adapter_app
    return TestClient(adapter_app)


def test_drain_is_blocked_when_signed_out(adapter):
    assert adapter.post("/api/v1/drain").status_code == 401


def test_drain_accepts_a_console_session(adapter):
    """Without this the console's "Drain queued" button 401s after login."""
    adapter.cookies.set(auth.COOKIE_NAME, auth.issue_token())
    assert adapter.post("/api/v1/drain").status_code == 200


def test_drain_still_accepts_the_internal_key(adapter):
    response = adapter.post(
        "/api/v1/drain", headers={"Authorization": f"Bearer {INTERNAL_KEY}"}
    )
    assert response.status_code == 200


def test_drain_rejects_a_forged_session(adapter):
    adapter.cookies.set(auth.COOKIE_NAME, "forged.token")
    assert adapter.post("/api/v1/drain").status_code == 401
