"""Every API response is JSON, including unhandled errors.

A plain-text 500 is what Starlette returns when an exception escapes a route.
The console then shows "Expected JSON, got 500 text/plain" on every panel with
no way to see the cause — which is exactly what an unreachable database
produced before these handlers existed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.api.server import _safe_message, app
from src.workspace import get_default_workspace_id, seed_default_workspace

PASSWORD = "errors-test-password"
AUTH_BODY = {"password": PASSWORD}

DATA_ROUTES = [
    "/api/v1/dashboard/summary",
    "/api/v1/leads",
    "/api/v1/runs",
    "/api/v1/generated-content",
    "/api/v1/settings/status",
    "/api/v1/prompts",
    "/api/v1/settings",
]


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    seed_default_workspace()
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/auth/login", json=AUTH_BODY)
    return client


# ---------------------------------------------------------------------------
# Empty database: zeros and empty lists, never an error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", DATA_ROUTES)
def test_data_routes_return_json_on_an_empty_database(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_dashboard_summary_is_all_zeros_when_empty(client):
    body = client.get("/api/v1/dashboard/summary").json()
    assert body["counts"]["leads_total"] == 0
    assert body["counts"]["enriched"] == 0
    assert body["counts"]["scored"] == 0
    assert body["counts"]["sent"] == 0
    assert body["counts"]["replied"] == 0
    assert body["recent_runs"] == []
    assert body["failed_runs"] == []
    assert body["recent_activity"] == []


def test_list_routes_are_empty_lists_not_nulls(client):
    assert client.get("/api/v1/leads").json()["leads"] == []
    assert client.get("/api/v1/runs").json()["runs"] == []
    assert client.get("/api/v1/generated-content").json()["content"] == []
    assert client.get("/api/v1/signals").json()["signals"] == []
    assert client.get("/api/v1/research").json()["research"] == []


def test_workspace_exists_after_seeding(client):
    """A missing default workspace is what makes queries return nothing."""
    assert get_default_workspace_id() is not None


# ---------------------------------------------------------------------------
# Unhandled errors become JSON
# ---------------------------------------------------------------------------

def test_unhandled_exception_returns_json_not_plain_text(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("something went badly wrong")

    monkeypatch.setattr("src.lib.db_queries.kpi_counts", boom)
    response = client.get("/api/v1/dashboard/summary")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"] == "internal_server_error"
    assert "something went badly wrong" in body["message"]
    assert body["route"] == "/api/v1/dashboard/summary"
    assert body["request_id"]


def test_database_errors_return_503_with_a_hint(client, monkeypatch):
    def unreachable(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr("src.lib.db_queries.kpi_counts", unreachable)
    response = client.get("/api/v1/dashboard/summary")

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "database_unavailable"
    assert "DATABASE_URL" in body["hint"]
    assert body["request_id"]


def test_error_responses_never_leak_the_connection_string(client, monkeypatch):
    def leaky(*args, **kwargs):
        raise RuntimeError(
            "could not connect to postgresql://postgres:hunter2@db.example:6543/postgres"
        )

    monkeypatch.setattr("src.lib.db_queries.kpi_counts", leaky)
    response = client.get("/api/v1/dashboard/summary")

    assert "hunter2" not in response.text
    assert "***:***@" in response.json()["message"]


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://user:secret@host:6543/db",
        "postgres://admin:p%40ss@aws-0-eu.pooler.supabase.com:6543/postgres",
        "mysql://root:toor@localhost/db",
    ],
)
def test_safe_message_redacts_any_connection_string(raw):
    assert "secret" not in _safe_message(Exception(raw))
    assert "p%40ss" not in _safe_message(Exception(raw))
    assert "toor" not in _safe_message(Exception(raw))


def test_invalid_request_body_returns_structured_json(client):
    response = client.post("/api/v1/leads/process", json={"leads": "not-a-list"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_request"
    assert body["route"] == "/api/v1/leads/process"


# ---------------------------------------------------------------------------
# Auth still shapes the response
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", DATA_ROUTES)
def test_data_routes_are_json_401_when_signed_out(monkeypatch, path):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    signed_out = TestClient(app, raise_server_exceptions=False)
    response = signed_out.get(path)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]
