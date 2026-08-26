"""The read APIs that back the operator console.

Every route here is workspace-scoped, read-only, and must never leak a secret
value. They exist so the Next.js console can render leads, runs and content
without a second query layer.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.server import app
from src.db import session_scope
from src.models import GeneratedContent, Lead, Score
from src.workspace import get_default_workspace_id, seed_default_workspace

KEY = "console-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", KEY)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def seeded() -> dict:
    """One workspace, two leads, one scored with a generated email."""
    seed_default_workspace()
    ws_id = get_default_workspace_id()
    with session_scope() as session:
        alice = Lead(
            first_name="Alice", last_name="Adams", email="alice@acme.test",
            title="VP Marketing", company="Acme", workspace_id=ws_id,
        )
        bob = Lead(
            first_name="Bob", last_name="Brown", email="bob@globex.test",
            title="Head of Ops", company="Globex", workspace_id=ws_id,
        )
        session.add_all([alice, bob])
        session.flush()
        session.add(Score(
            lead_id=alice.id, score=91, tier="A", rationale="Strong ICP fit",
            model="test-model", workspace_id=ws_id,
        ))
        session.add(GeneratedContent(
            lead_id=alice.id, kind="email", subject="Quick question, Alice",
            body="Hi Alice — noticed Acme is hiring.", prompt_version="v1",
            model="test-model", workspace_id=ws_id,
        ))
        return {"ws": ws_id, "alice": alice.id, "bob": bob.id}


# ---------------------------------------------------------------------------
# Auth — every console route is protected
# ---------------------------------------------------------------------------

CONSOLE_ROUTES = [
    "/api/v1/dashboard/summary",
    "/api/v1/leads",
    "/api/v1/leads/1",
    "/api/v1/runs",
    "/api/v1/generated-content",
    "/api/v1/settings/status",
]


@pytest.mark.parametrize("path", CONSOLE_ROUTES)
def test_console_routes_require_the_api_key(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", CONSOLE_ROUTES)
def test_console_routes_reject_a_wrong_key(client, path):
    response = client.get(path, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------

def test_dashboard_summary_shape(client, seeded):
    body = client.get("/api/v1/dashboard/summary", headers=AUTH).json()
    assert body["counts"]["leads_total"] == 2
    assert body["tiers"].get("A") == 1
    for key in ("recent_activity", "recent_runs", "failed_runs", "ready_to_send"):
        assert key in body


def test_dashboard_summary_on_an_empty_database(client):
    """An empty workspace must render, not error."""
    seed_default_workspace()
    body = client.get("/api/v1/dashboard/summary", headers=AUTH).json()
    assert body["counts"]["leads_total"] == 0
    assert body["recent_runs"] == []


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def test_lead_list_returns_records_and_total(client, seeded):
    body = client.get("/api/v1/leads", headers=AUTH).json()
    assert body["total"] == 2
    assert {row["Name"] for row in body["leads"]} == {"Alice Adams", "Bob Brown"}


def test_lead_list_search_filter(client, seeded):
    body = client.get("/api/v1/leads?search=globex", headers=AUTH).json()
    assert body["total"] == 1
    assert body["leads"][0]["Company"] == "Globex"


def test_lead_list_tier_filter(client, seeded):
    body = client.get("/api/v1/leads?tier=A", headers=AUTH).json()
    assert [row["Tier"] for row in body["leads"]] == ["A"]


def test_lead_list_pagination_is_bounded(client, seeded):
    body = client.get("/api/v1/leads?limit=1&offset=0", headers=AUTH).json()
    assert len(body["leads"]) == 1
    assert body["total"] == 2
    # A caller cannot ask for an unbounded page.
    assert client.get("/api/v1/leads?limit=9999", headers=AUTH).json()["limit"] == 200


def test_lead_detail_includes_score_and_content(client, seeded):
    body = client.get(f"/api/v1/leads/{seeded['alice']}", headers=AUTH).json()
    assert body["lead"]["email"] == "alice@acme.test"
    assert body["score"]["tier"] == "A"
    assert body["contents"][0]["subject"] == "Quick question, Alice"


def test_lead_detail_404s_for_an_unknown_lead(client, seeded):
    assert client.get("/api/v1/leads/999999", headers=AUTH).status_code == 404


def test_lead_detail_is_workspace_scoped(client, seeded):
    """A lead must not be readable through another workspace's id."""
    response = client.get(
        f"/api/v1/leads/{seeded['alice']}?workspace_id=987654", headers=AUTH
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Runs and content
# ---------------------------------------------------------------------------

def test_runs_list_is_empty_not_an_error(client, seeded):
    assert client.get("/api/v1/runs", headers=AUTH).json()["runs"] == []


def test_generated_content_list(client, seeded):
    body = client.get("/api/v1/generated-content", headers=AUTH).json()
    assert len(body["content"]) == 1
    item = body["content"][0]
    assert item["subject"] == "Quick question, Alice"
    assert item["lead_name"] == "Alice Adams"
    assert item["company"] == "Acme"


def test_generated_content_filters_by_kind(client, seeded):
    body = client.get("/api/v1/generated-content?kind=call_script", headers=AUTH).json()
    assert body["content"] == []


# ---------------------------------------------------------------------------
# Settings status — presence only, never values
# ---------------------------------------------------------------------------

def test_settings_status_reports_presence_not_values(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SUPER-SECRET-VALUE")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    response = client.get("/api/v1/settings/status", headers=AUTH)
    body = response.json()

    assert body["env"]["ANTHROPIC_API_KEY"] is True
    assert body["env"]["TAVILY_API_KEY"] is False
    # The value itself must never appear anywhere in the response.
    assert "SUPER-SECRET-VALUE" not in response.text


def test_settings_status_exposes_scoring_config(client):
    body = client.get("/api/v1/settings/status", headers=AUTH).json()
    assert body["scoring"]["tier_a_min"] == 85
    assert body["scoring"]["send_min_tier"] in {"A", "B", "C", "D"}
    assert body["scoring"]["email_verifier"] in {
        "instantly", "neverbounce", "millionverifier",
    }
