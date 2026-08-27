"""Prompt section editing and the workspace settings editor.

The console edits a prompt one `# SECTION` at a time and recombines on save, so
the split/compile pair must round-trip exactly — a lossy round trip would
silently rewrite the prompt the generator uses.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.server import app
from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY
from src.prompts.sections import compile_sections, split_sections
from src.workspace import seed_default_workspace

KEY = "prompt-editor-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", KEY)


@pytest.fixture
def client() -> TestClient:
    seed_default_workspace()
    return TestClient(app)


# ---------------------------------------------------------------------------
# split / compile
# ---------------------------------------------------------------------------

def test_round_trip_is_lossless_for_the_real_prompt():
    sections = split_sections(DEFAULT_EMAIL_PROMPT_BODY)
    assert len(sections) > 5, "the default email prompt should split into sections"
    assert compile_sections(sections).strip() == DEFAULT_EMAIL_PROMPT_BODY.strip()


def test_sections_come_from_the_prompt_not_a_fixed_list():
    """A section the prompt has must survive even if it is not 'expected'."""
    text = "# ONE\nalpha\n\n# UNEXPECTED SECTION\nbeta\n"
    titles = [s["title"] for s in split_sections(text)]
    assert titles == ["ONE", "UNEXPECTED SECTION"]


def test_text_before_the_first_header_is_preserved():
    text = "preamble line\n\n# ONE\nalpha\n"
    sections = split_sections(text)
    assert sections[0]["title"] == ""
    assert "preamble line" in sections[0]["body"]
    assert "preamble line" in compile_sections(sections)


def test_empty_input_produces_no_sections():
    assert split_sections("") == []


def test_editing_one_section_leaves_the_others_untouched():
    sections = split_sections(DEFAULT_EMAIL_PROMPT_BODY)
    original_second = sections[1]["body"]
    sections[0]["body"] = "replaced"
    rebuilt = compile_sections(sections)
    assert "replaced" in rebuilt
    assert original_second in rebuilt


# ---------------------------------------------------------------------------
# /api/v1/prompts/editor
# ---------------------------------------------------------------------------

def test_editor_requires_the_api_key(client):
    assert client.get("/api/v1/prompts/editor?channel=email").status_code == 401


def test_editor_rejects_an_unknown_channel(client):
    response = client.get("/api/v1/prompts/editor?channel=carrier-pigeon", headers=AUTH)
    assert response.status_code == 422


@pytest.mark.parametrize("channel", ["email", "linkedin_msg", "call_script"])
def test_every_channel_has_a_default_to_edit(client, channel):
    """A channel whose default constant is misnamed returns zero sections."""
    body = client.get(f"/api/v1/prompts/editor?channel={channel}", headers=AUTH).json()
    assert body["sections"], f"{channel} resolved no prompt sections"
    assert body["compiled"].strip()


def test_saving_sections_persists_and_reloads(client):
    before = client.get("/api/v1/prompts/editor?channel=email", headers=AUTH).json()
    sections = before["sections"]
    sections[0]["body"] += "\nAN EDIT FROM THE CONSOLE"

    saved = client.post(
        "/api/v1/prompts/editor",
        headers=AUTH,
        json={"channel": "email", "sections": sections},
    ).json()
    assert saved["saved"] is True
    assert saved["metadata"]["updated_by"] == "web-app"

    after = client.get("/api/v1/prompts/editor?channel=email", headers=AUTH).json()
    assert "AN EDIT FROM THE CONSOLE" in after["sections"][0]["body"]
    assert after["source"] == "database"


def test_saving_empty_sections_is_rejected(client):
    response = client.post(
        "/api/v1/prompts/editor", headers=AUTH, json={"channel": "email", "sections": []}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /api/v1/settings
# ---------------------------------------------------------------------------

def test_settings_requires_the_api_key(client):
    assert client.get("/api/v1/settings").status_code == 401
    assert client.post("/api/v1/settings", json={"config": {}}).status_code == 401


def test_settings_returns_the_editable_config(client):
    body = client.get("/api/v1/settings", headers=AUTH).json()
    for key in ("company", "icp", "persona", "signals", "news_search_terms"):
        assert key in body["config"], f"settings is missing {key}"
    assert "workspace" in body and "deliverability" in body


def test_settings_never_returns_a_secret_value(client, monkeypatch):
    monkeypatch.setenv("INSTANTLY_API_KEY", "inst-SUPER-SECRET-VALUE")
    response = client.get("/api/v1/settings", headers=AUTH)
    assert "SUPER-SECRET-VALUE" not in response.text


def test_saving_settings_merges_rather_than_replacing(client):
    before = client.get("/api/v1/settings", headers=AUTH).json()["config"]
    original_one_liner = before["company"]["one_liner"]

    saved = client.post(
        "/api/v1/settings",
        headers=AUTH,
        json={"config": {"company": {"name": "SignalOS"}}},
    ).json()
    assert saved["saved"] is True
    assert saved["config"]["company"]["name"] == "SignalOS"
    # A partial save of one accordion must not wipe the sibling fields.
    assert saved["config"]["company"]["one_liner"] == original_one_liner


def test_saving_invalid_settings_is_rejected(client):
    response = client.post(
        "/api/v1/settings",
        headers=AUTH,
        json={"config": {"news_search_terms": "should be a list"}},
    )
    assert response.status_code == 422
