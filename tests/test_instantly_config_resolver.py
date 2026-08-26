"""Hotfix: Instantly push uses workspace CAMPAIGN id + env/secret API KEY.

The Instantly API key is shared infrastructure (env var / Streamlit secret /
app config) and is NEVER required on the workspace. The campaign id is the
per-workspace routing value. These tests pin the resolver + the push path to
that model and prove safety gates are not weakened.

Required matrix:
  1.  Env INSTANTLY_API_KEY is used.
  2.  Streamlit secret INSTANTLY_API_KEY is used when env missing.
  3.  Workspace campaign_id column is used.
  4.  Env campaign id fallback works if workspace campaign id missing.
  5.  API key is not required on the workspace.
  6.  Missing API key returns missing_instantly_api_key only.
  7.  Missing campaign id returns missing_instantly_campaign_id only.
  8.  Full API key is never logged.
  9.  Push uses selected workspace campaign id + env/secret API key.
  10. Safety checks remain enforced (unsafe content blocked even with good config).
  11. A failed config check does not mark leads as sent.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

import src.delivery.instantly as inst
import src.delivery.instantly_config as cfgmod
from src.db import session_scope
from src.delivery.instantly_config import build_instantly_diagnostic, resolve_instantly_config
from src.delivery.verify_email import VerifyResult
from src.models import GeneratedContent, Lead, Score, Workspace
from src.workspace import create_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_env(monkeypatch) -> None:
    """Ensure a deterministic env/config baseline for credential resolution."""
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.delenv("INSTANTLY_CAMPAIGN_ID", raising=False)
    monkeypatch.setattr(cfgmod.settings, "instantly_api_key", "", raising=False)
    monkeypatch.setattr(cfgmod.settings, "instantly_campaign_id", "", raising=False)




def _ws_without_campaign(slug: str) -> int:
    with session_scope() as session:
        ws = Workspace(name=slug, slug=slug, is_default=False, is_active=True)
        session.add(ws)
        session.flush()
        return ws.id


def _seed_eligible_lead(ws_id: int, email: str, *, body: str = "Personalized body text.") -> int:
    with session_scope() as session:
        lead = Lead(first_name="A", last_name="B", email=email, workspace_id=ws_id)
        session.add(lead)
        session.flush()
        session.add(Score(
            lead_id=lead.id, score=92, tier="A", rationale="r",
            signals_used=[], model="x", workspace_id=ws_id,
        ))
        session.add(GeneratedContent(
            lead_id=lead.id, kind="email", subject="Subject", body=body,
            prompt_version="v1", model="x", workspace_id=ws_id,
        ))
        session.flush()
        return lead.id


# ===========================================================================
# 1. Env INSTANTLY_API_KEY is used
# ===========================================================================

def test_env_api_key_is_used(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-key-123")
    ws = create_workspace("Env Key", "env-key-ws", "camp-e1")

    cfg = resolve_instantly_config(ws["id"])
    assert cfg.api_key == "env-key-123"
    assert cfg.api_key_source == "env"


# ===========================================================================
# 2. Resolution precedence
# ===========================================================================


def test_workspace_campaign_precedes_env(monkeypatch):
    """Workspace campaign id wins over the env fallback."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-key")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "env-camp")
    ws = create_workspace("WS Wins", "ws-wins", "workspace-camp")

    cfg = resolve_instantly_config(ws["id"])
    assert cfg.campaign_id == "workspace-camp"
    assert cfg.campaign_id_source == "workspace_column"


def test_diagnostic_reports_sources_and_masks_key(monkeypatch):
    _clean_env(monkeypatch)
    secret = "sk-FULLSECRET-zzz-999-do-not-leak"
    monkeypatch.setenv("INSTANTLY_API_KEY", secret)
    ws = create_workspace("Diag", "diag-ws", "camp-diag")

    diag = build_instantly_diagnostic(ws["id"])
    assert diag["api_key_found"] is True
    assert diag["api_key_source"] == "env"
    assert diag["campaign_id_found"] is True
    assert diag["campaign_id_source"] == "workspace_column"  # workspace wins
    assert diag["probes"]["env_INSTANTLY_API_KEY"] is True
    assert diag["probes"]["config_instantly_api_key"] is False
    # The full key is NEVER present in the diagnostic.
    assert secret not in str(diag)
    assert diag["api_key_masked"].startswith("sk-F")


def test_generic_combined_error_removed():
    """The vague combined error must be gone from the push paths."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    vague = "Instantly API key or campaign ID not configured for this workspace"
    for rel in ("src/delivery/instantly.py", "src/lib/bulk_push.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert vague not in text, f"{rel} still contains the vague combined error"


# ===========================================================================
# 3. Workspace campaign_id column is used
# ===========================================================================

def test_workspace_campaign_id_column_is_used(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-key")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "env-campaign-should-not-win")
    ws = create_workspace("WS Camp", "ws-camp", "39d7857e-7246-43f1-9fa0-f351542c2e87")

    cfg = resolve_instantly_config(ws["id"])
    assert cfg.campaign_id == "39d7857e-7246-43f1-9fa0-f351542c2e87"
    assert cfg.campaign_id_source == "workspace_column"


# ===========================================================================
# 4. Env campaign fallback works if workspace campaign id missing
# ===========================================================================

def test_env_campaign_fallback_when_workspace_campaign_missing(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-key")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "env-campaign-77")
    ws_id = _ws_without_campaign("no-camp-ws")

    cfg = resolve_instantly_config(ws_id, allow_campaign_env_fallback=True)
    assert cfg.campaign_id == "env-campaign-77"
    assert cfg.campaign_id_source == "env"


# ===========================================================================
# 5. API key is not required on the workspace (env wins, ws key ignored)
# ===========================================================================

def test_api_key_not_required_on_workspace(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-key-wins")
    # Even a workspace that stored its own key must NOT override env/secret.
    ws = create_workspace("Has WS Key", "has-ws-key", "camp-w", instantly_api_key="ws-stored-key")

    cfg = resolve_instantly_config(ws["id"])
    assert cfg.api_key == "env-key-wins"
    assert cfg.api_key_source == "env"
    assert cfg.api_key != "ws-stored-key"


# ===========================================================================
# 6. Missing API key returns missing_instantly_api_key only
# ===========================================================================

def test_missing_api_key_only(monkeypatch):
    _clean_env(monkeypatch)  # no env/secret/config key
    ws = create_workspace("Camp But No Key", "camp-no-key", "camp-present")

    cfg = resolve_instantly_config(ws["id"])
    assert cfg.api_key is None
    assert cfg.campaign_id == "camp-present"
    assert cfg.missing_reasons == ["missing_instantly_api_key"]


# ===========================================================================
# 7. Missing campaign id returns missing_instantly_campaign_id only
# ===========================================================================

def test_missing_campaign_id_only(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-key")
    ws_id = _ws_without_campaign("key-but-no-camp")

    cfg = resolve_instantly_config(ws_id, allow_campaign_env_fallback=False)
    assert cfg.api_key == "env-key"
    assert cfg.campaign_id is None
    assert cfg.missing_reasons == ["missing_instantly_campaign_id"]


# ===========================================================================
# 8. Full API key is never logged
# ===========================================================================

def test_full_api_key_never_logged(monkeypatch, caplog):
    _clean_env(monkeypatch)
    secret = "sk-supersecret-FULL-KEY-abcdef0123456789"
    monkeypatch.setenv("INSTANTLY_API_KEY", secret)
    ws = create_workspace("Log Safe", "log-safe", "camp-l")

    with caplog.at_level(logging.INFO, logger="src.delivery.instantly_config"):
        cfg = resolve_instantly_config(ws["id"])

    assert cfg.api_key == secret
    # The full key must appear nowhere in any captured log record.
    for rec in caplog.records:
        assert secret not in rec.getMessage()
        masked = getattr(rec, "api_key_masked", "")
        assert secret not in str(masked)
    # And the resolver records found a masked prefix (not the whole key).
    masked_vals = [getattr(r, "api_key_masked", "") for r in caplog.records]
    assert any(str(m).startswith("sk-s") and secret not in str(m) for m in masked_vals)


# ===========================================================================
# 9. Push uses selected workspace campaign + env/secret API key
# ===========================================================================

def test_push_uses_workspace_campaign_and_env_key(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-push-key")
    ws = create_workspace("Push WS", "push-ws", "camp-push-1")
    lead_id = _seed_eligible_lead(ws["id"], "push@example.com")

    captured: dict[str, tuple] = {}

    async def fake_verify(_lead_id, **_kw):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(payload, api_key=None):
        captured["post"] = (payload["campaign"], api_key)
        return {"id": "remote-x"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    res = asyncio.run(inst.deliver_email(lead_id, dry_run=False, workspace_id=ws["id"]))
    assert res.delivered is True
    assert captured["post"] == ("camp-push-1", "env-push-key")


# ===========================================================================
# 10. Safety checks remain enforced (unsafe content blocked despite good config)
# ===========================================================================

def test_safety_gate_enforced_even_with_valid_config(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "env-push-key")
    ws = create_workspace("Safe Cfg", "safe-cfg", "camp-safe")
    lead_id = _seed_eligible_lead(
        ws["id"], "unsafe@example.com",
        body="NEEDS REVIEW: internal placeholder — do not send.",
    )

    posted = {"hit": False}

    async def fake_verify(_lead_id, **_kw):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(*a, **k):
        posted["hit"] = True
        return {"id": "x"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    res = asyncio.run(inst.deliver_email(lead_id, dry_run=False, workspace_id=ws["id"]))
    assert res.delivered is False
    assert res.skip_reason == "unsafe_internal_review_content"
    assert posted["hit"] is False


# ===========================================================================
# 11. A failed config check does not mark leads as sent
# ===========================================================================

def test_failed_config_does_not_mark_sent(monkeypatch):
    _clean_env(monkeypatch)  # NO api key anywhere → config check fails
    ws = create_workspace("No Key WS", "no-key-ws", "camp-nokey")
    lead_id = _seed_eligible_lead(ws["id"], "nokey@example.com")

    posted = {"hit": False}

    async def fake_verify(_lead_id, **_kw):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(*a, **k):
        posted["hit"] = True
        return {"id": "x"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    res = asyncio.run(inst.deliver_email(lead_id, dry_run=False, workspace_id=ws["id"]))
    assert res.delivered is False
    assert res.skip_reason == "missing_instantly_api_key"
    assert posted["hit"] is False

    # The content row must NOT be marked delivered/sent.
    with session_scope() as session:
        contents = session.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == lead_id)
        ).scalars().all()
        for c in contents:
            assert c.delivered_at is None
            assert (c.delivery_status != "sent") if c.delivery_status else True
