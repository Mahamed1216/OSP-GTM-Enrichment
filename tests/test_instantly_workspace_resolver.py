"""Instantly push credential model: per-workspace CAMPAIGN id + shared
env/secret API KEY.

Hotfix model: the API key is shared infrastructure (env / Streamlit secret /
app config) and is NEVER stored per workspace. The campaign id is the
per-workspace routing value. So each workspace's push uses its OWN campaign with
the SAME shared key — workspace isolation is preserved via the campaign id, and
an operator never has to store a key per workspace.

These tests prove each push flow (deliver_email / overwrite_lead_copy /
debug_overwrite_one_lead) threads the workspace campaign id + the shared
env/secret key through every HTTP call, and that an explicitly-selected
workspace with no campaign fails clearly instead of using the env/default
campaign.
"""
from __future__ import annotations

import asyncio

from src.db import session_scope
from src.delivery import instantly as inst
from src.delivery.verify_email import VerifyResult
from src.models import GeneratedContent, Lead, Score, Workspace
from src.workspace import create_workspace


def _seed_lead_with_email(workspace_id: int, email: str) -> int:
    """Create a lead + one email GeneratedContent row in the given workspace."""
    with session_scope() as session:
        lead = Lead(
            first_name="A", last_name="B", email=email, workspace_id=workspace_id
        )
        session.add(lead)
        session.flush()
        session.add(GeneratedContent(
            lead_id=lead.id,
            kind="email",
            subject="Subject",
            body="Personalized body text.",
            prompt_version="v1",
            model="claude-sonnet-4-6",
            workspace_id=workspace_id,
        ))
        session.flush()
        return lead.id


def _seed_eligible_lead(workspace_id: int, email: str) -> int:
    """Lead that passes all deliver_email guards: tier-A score + email content."""
    with session_scope() as session:
        lead = Lead(
            first_name="A", last_name="B", email=email, workspace_id=workspace_id
        )
        session.add(lead)
        session.flush()
        session.add(Score(
            lead_id=lead.id, score=92, tier="A", rationale="r",
            signals_used=[], model="x", workspace_id=workspace_id,
        ))
        session.add(GeneratedContent(
            lead_id=lead.id, kind="email", subject="Subject",
            body="Personalized body text.", prompt_version="v1",
            model="claude-sonnet-4-6", workspace_id=workspace_id,
        ))
        session.flush()
        return lead.id


def test_deliver_email_uses_workspace_specific_credentials(monkeypatch):
    """The bulk live-send path calls deliver_email(..., workspace_id=active).
    Proves deliver_email posts to the active workspace's OWN campaign with the
    SHARED env/secret API key — so a send in workspace B uses workspace B's
    campaign, never workspace A's, while both share the env key.
    """
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "shared-env-key")

    ws1 = create_workspace("Send One", "send-one", "camp-s1")
    ws2 = create_workspace("Send Two", "send-two", "camp-s2")
    lead1 = _seed_eligible_lead(ws1["id"], "s1@example.com")
    lead2 = _seed_eligible_lead(ws2["id"], "s2@example.com")

    captured: dict[str, tuple] = {}

    async def fake_verify(_lead_id, **_kw):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(payload, api_key=None):
        captured["post"] = (payload["campaign"], api_key)
        return {"id": "remote-x"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    res1 = asyncio.run(inst.deliver_email(lead1, dry_run=False, workspace_id=ws1["id"]))
    assert res1.delivered is True
    assert captured["post"] == ("camp-s1", "shared-env-key")

    res2 = asyncio.run(inst.deliver_email(lead2, dry_run=False, workspace_id=ws2["id"]))
    assert res2.delivered is True
    assert captured["post"] == ("camp-s2", "shared-env-key")


def test_overwrite_lead_copy_uses_workspace_specific_credentials(monkeypatch):
    """Two workspaces with distinct campaigns → overwrite uses each workspace's
    OWN campaign with the shared env/secret key, on both lookup and create."""
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "shared-env-key")

    ws1 = create_workspace("Client One", "client-one", "camp-one")
    ws2 = create_workspace("Client Two", "client-two", "camp-two")
    lead1 = _seed_lead_with_email(ws1["id"], "one@example.com")
    lead2 = _seed_lead_with_email(ws2["id"], "two@example.com")

    captured: dict[str, tuple] = {}

    async def fake_find(email, campaign_id, api_key=None):
        captured["find"] = (campaign_id, api_key)
        return []  # no match → exercises the create path

    async def fake_post(payload, api_key=None):
        captured["post"] = (payload["campaign"], api_key)
        return {"id": "remote-x"}

    async def fake_get(remote_id, api_key=None):
        return {}

    monkeypatch.setattr(inst, "find_leads_by_email", fake_find)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)
    monkeypatch.setattr(inst, "get_lead", fake_get)

    res1 = asyncio.run(inst.overwrite_lead_copy(lead1, workspace_id=ws1["id"]))
    assert res1.status == "created"
    assert captured["find"] == ("camp-one", "shared-env-key")
    assert captured["post"] == ("camp-one", "shared-env-key")

    res2 = asyncio.run(inst.overwrite_lead_copy(lead2, workspace_id=ws2["id"]))
    assert res2.status == "created"
    assert captured["find"] == ("camp-two", "shared-env-key")
    assert captured["post"] == ("camp-two", "shared-env-key")


def test_overwrite_update_path_uses_workspace_credentials(monkeypatch):
    """When the lead already exists in the campaign (1 match), the PATCH +
    read-back calls also carry the shared env/secret key."""
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "shared-env-key")

    ws = create_workspace("Upd One", "upd-one", "camp-u1")
    lead = _seed_lead_with_email(ws["id"], "u1@example.com")

    captured: dict[str, object] = {}

    async def fake_find(email, campaign_id, api_key=None):
        captured["find_key"] = api_key
        return [{"id": "remote-1", "email": email}]  # exactly one match → PATCH path

    async def fake_patch(remote_lead_id, payload, api_key=None):
        captured["patch_key"] = api_key
        return {}

    async def fake_get(remote_id, api_key=None):
        captured["get_key"] = api_key
        # Read-back returns the values we "wrote" so the flow reports "updated".
        return {"payload": {
            "personalized_subject": "Subject",
            "personalized_body": "Personalized body text.",
        }}

    monkeypatch.setattr(inst, "find_leads_by_email", fake_find)
    monkeypatch.setattr(inst, "patch_lead", fake_patch)
    monkeypatch.setattr(inst, "get_lead", fake_get)
    # _mark_local_sent writes to the DB; let it run normally.

    res = asyncio.run(inst.overwrite_lead_copy(lead, workspace_id=ws["id"]))
    assert res.status == "updated"
    assert captured["find_key"] == "shared-env-key"
    assert captured["patch_key"] == "shared-env-key"
    assert captured["get_key"] == "shared-env-key"


def test_debug_overwrite_one_lead_uses_workspace_specific_credentials(monkeypatch):
    """The diagnostic flow hits the workspace's campaign with the shared key."""
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "shared-env-key")

    ws1 = create_workspace("Dbg One", "dbg-one", "camp-d1")
    ws2 = create_workspace("Dbg Two", "dbg-two", "camp-d2")
    lead1 = _seed_lead_with_email(ws1["id"], "d1@example.com")
    lead2 = _seed_lead_with_email(ws2["id"], "d2@example.com")

    calls: list[tuple] = []

    async def fake_raw(method, url, *, json_body=None, api_key=None):
        calls.append((method, api_key, (json_body or {}).get("campaign")))
        # Return a search response with no matches so the flow ends early.
        return {"status": 200, "response_json": {"items": []}}

    monkeypatch.setattr(inst, "_raw_request", fake_raw)

    out1 = asyncio.run(inst.debug_overwrite_one_lead(lead1, workspace_id=ws1["id"]))
    assert out1["campaign_id"] == "camp-d1"
    # First (search) call carried the shared key + the workspace's campaign.
    assert calls[0] == ("POST", "shared-env-key", "camp-d1")

    calls.clear()
    out2 = asyncio.run(inst.debug_overwrite_one_lead(lead2, workspace_id=ws2["id"]))
    assert out2["campaign_id"] == "camp-d2"
    assert calls[0] == ("POST", "shared-env-key", "camp-d2")


def _seed_workspace_without_credentials(slug: str) -> int:
    with session_scope() as session:
        ws = Workspace(name=slug, slug=slug, is_default=False, is_active=True)
        session.add(ws)
        session.flush()
        return ws.id


def test_overwrite_explicit_workspace_missing_config_does_not_fallback_to_env(monkeypatch):
    """Explicit workspace with no campaign/key must fail clearly and NOT use the
    env/default account — even when env credentials ARE set."""
    ws_id = _seed_workspace_without_credentials("no-creds-ovr")
    lead = _seed_lead_with_email(ws_id, "nc-ovr@example.com")

    # Env campaign IS configured — the fix must refuse to use it for an explicit
    # workspace. The shared API key resolves fine; only the campaign is missing.
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "ENV-KEY")
    monkeypatch.setattr(inst.settings, "instantly_campaign_id", "ENV-CAMP")

    called = {"hit": False}

    async def fake_find(*args, **kwargs):
        called["hit"] = True
        return []

    async def fake_post(*args, **kwargs):
        called["hit"] = True
        return {"id": "remote-x"}

    monkeypatch.setattr(inst, "find_leads_by_email", fake_find)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    res = asyncio.run(inst.overwrite_lead_copy(lead, workspace_id=ws_id))
    assert res.status == "failed"
    # API key present (env), campaign missing → only the campaign reason.
    assert "missing_instantly_campaign_id" in (res.detail or "")
    assert "missing_instantly_api_key" not in (res.detail or "")
    assert called["hit"] is False  # no Instantly call — env campaign not used


def test_debug_explicit_workspace_missing_config_does_not_fallback_to_env(monkeypatch):
    """Diagnostic flow on an explicit workspace with no config fails clearly and
    makes no Instantly call, even with env credentials present."""
    ws_id = _seed_workspace_without_credentials("no-creds-dbg")
    lead = _seed_lead_with_email(ws_id, "nc-dbg@example.com")

    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "ENV-KEY")
    monkeypatch.setattr(inst.settings, "instantly_campaign_id", "ENV-CAMP")

    called = {"hit": False}

    async def fake_raw(*args, **kwargs):
        called["hit"] = True
        return {"status": 200, "response_json": {}}

    monkeypatch.setattr(inst, "_raw_request", fake_raw)

    out = asyncio.run(inst.debug_overwrite_one_lead(lead, workspace_id=ws_id))
    assert "missing_instantly_campaign_id" in (out["verdict"] or "")
    assert out["campaign_id"] != "ENV-CAMP"  # did not adopt the env campaign
    assert called["hit"] is False  # no Instantly call — env not used


def test_deliver_email_explicit_workspace_missing_config_does_not_fallback_to_env(monkeypatch):
    """A fully eligible lead in an explicit workspace with no Instantly config is
    skipped (workspace_not_configured) and never posted to the env/default campaign."""
    ws_id = _seed_workspace_without_credentials("no-creds-del")
    lead = _seed_eligible_lead(ws_id, "nc-del@example.com")

    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "ENV-KEY")
    monkeypatch.setattr(inst.settings, "instantly_campaign_id", "ENV-CAMP")

    posted = {"hit": False}

    async def fake_verify(_lead_id, **_kw):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(*args, **kwargs):
        posted["hit"] = True
        return {"id": "remote-x"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    res = asyncio.run(inst.deliver_email(lead, dry_run=False, workspace_id=ws_id))
    assert res.delivered is False
    assert res.skip_reason == "workspace_not_configured"
    assert posted["hit"] is False  # never sent through the env/default campaign


def test_deliver_email_none_workspace_preserves_env_fallback(monkeypatch):
    """workspace_id=None (CLI / single-workspace) still falls back to env when no
    default workspace provides credentials — preserving existing behavior."""
    # No default workspace is seeded in the test DB, so resolution falls to env.
    ws_id = _seed_workspace_without_credentials("any-ws-none")
    lead = _seed_eligible_lead(ws_id, "none-fallback@example.com")

    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.delenv("INSTANTLY_CAMPAIGN_ID", raising=False)
    monkeypatch.setattr(inst.settings, "instantly_api_key", "ENV-KEY")
    monkeypatch.setattr(inst.settings, "instantly_campaign_id", "ENV-CAMP")

    captured: dict[str, tuple] = {}

    async def fake_verify(_lead_id, **_kw):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(payload, api_key=None):
        captured["post"] = (payload["campaign"], api_key)
        return {"id": "remote-x"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    res = asyncio.run(inst.deliver_email(lead, dry_run=False, workspace_id=None))
    assert res.delivered is True
    assert captured["post"] == ("ENV-CAMP", "ENV-KEY")  # env fallback preserved
