"""Single source of truth for resolving Instantly PUSH credentials.

Config model (hotfix):
  - Instantly API KEY is shared infrastructure — it lives in the environment /
    Streamlit secrets / app config, NEVER per workspace. It is therefore the
    same for every workspace (one Instantly account).
  - Instantly CAMPAIGN ID is the per-workspace routing value: each workspace
    sends into its own campaign. Workspace isolation is preserved here.

This deliberately does NOT read an API key from the workspace row, so an
operator never has to store a key per workspace. (The older
``workspace.get_api_key_for_workspace`` helper still exists for other callers;
the push path uses THIS resolver.)

API key resolution order:
  A. environment variable   INSTANTLY_API_KEY
  B. Streamlit secret        INSTANTLY_API_KEY
  C. app config (settings)   settings.instantly_api_key  (itself loads env/.env)

Campaign ID resolution order:
  A. workspace.instantly_campaign_id  (workspace column)
  B. workspace config campaign id      (workspace.icp_config["instantly_campaign_id"], if present)
  C. environment variable   INSTANTLY_CAMPAIGN_ID
  D. Streamlit secret        INSTANTLY_CAMPAIGN_ID

The env/secret campaign fallback (C/D) is gated by ``allow_campaign_env_fallback``
so the push path can require an explicitly-selected workspace to resolve its OWN
campaign (never silently sending through the env/default account — an existing
safety behavior we keep).

Nothing here ever sends, pushes, or mutates a lead — it only resolves config.
Secrets are never logged in full; only a short prefix + length is emitted.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from src.config import settings

log = logging.getLogger(__name__)

MISSING_API_KEY = "missing_instantly_api_key"
MISSING_CAMPAIGN_ID = "missing_instantly_campaign_id"


def _mask_key(key: str | None) -> str:
    """Display-safe: a short PREFIX + length only. Never the full key."""
    if not key:
        return "<none>"
    k = str(key)
    return f"{k[:4]}…(len={len(k)})"


def _read_streamlit_secret(name: str) -> str | None:
    """Best-effort read of a Streamlit secret. Returns None outside a Streamlit
    runtime, or when the secret / secrets.toml is absent. Never raises."""
    try:
        import streamlit as st
    except Exception:
        return None
    try:
        val = st.secrets.get(name)  # type: ignore[attr-defined]
    except Exception:
        # No secrets file, or st.secrets not initialised — degrade silently.
        return None
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _resolve_api_key() -> tuple[str | None, str]:
    """Return (api_key, source). source ∈ env|streamlit_secrets|config|missing.

    NEVER reads the workspace — the API key is shared env/secrets infrastructure.
    """
    env_key = (os.environ.get("INSTANTLY_API_KEY") or "").strip()
    if env_key:
        return env_key, "env"
    sec = _read_streamlit_secret("INSTANTLY_API_KEY")
    if sec:
        return sec, "streamlit_secrets"
    cfg_key = (settings.instantly_api_key or "").strip()
    if cfg_key:
        return cfg_key, "config"
    return None, "missing"


def _resolve_campaign_id_from_ws(
    workspace: dict | None, *, allow_env_fallback: bool
) -> tuple[str | None, str]:
    """Return (campaign_id, source).

    source ∈ workspace_column|workspace_config|env|streamlit_secrets|missing.
    """
    if workspace:
        col = (workspace.get("instantly_campaign_id") or "").strip()
        if col:
            return col, "workspace_column"
        cfg = workspace.get("icp_config") or {}
        if isinstance(cfg, dict):
            wc = (cfg.get("instantly_campaign_id") or "").strip()
            if wc:
                return wc, "workspace_config"

    if allow_env_fallback:
        env_cid = (
            os.environ.get("INSTANTLY_CAMPAIGN_ID")
            or settings.instantly_campaign_id
            or ""
        ).strip()
        if env_cid:
            return env_cid, "env"
        sec = _read_streamlit_secret("INSTANTLY_CAMPAIGN_ID")
        if sec:
            return sec, "streamlit_secrets"

    return None, "missing"


@dataclass
class InstantlyConfig:
    api_key: str | None
    api_key_source: str            # env | streamlit_secrets | config | missing
    campaign_id: str | None
    campaign_id_source: str        # workspace_column | workspace_config | env | streamlit_secrets | missing
    missing_reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_reasons


def resolve_instantly_config(
    workspace_id: int | None = None,
    *,
    allow_campaign_env_fallback: bool = True,
) -> InstantlyConfig:
    """Resolve the API key (env/secrets/config) + campaign id (workspace→env/secrets).

    Returns an InstantlyConfig with `missing_reasons` listing exactly which
    piece is absent (missing_instantly_api_key and/or missing_instantly_campaign_id).
    Logs a secret-safe debug record. Never raises.
    """
    # Resolve the workspace once for both slug (logging) and campaign id.
    workspace: dict | None = None
    try:
        from src.workspace import get_default_workspace, get_workspace_by_id
        workspace = (
            get_workspace_by_id(workspace_id) if workspace_id is not None
            else get_default_workspace()
        )
    except Exception:
        workspace = None
    workspace_slug = (workspace or {}).get("slug")

    api_key, api_key_source = _resolve_api_key()
    campaign_id, campaign_id_source = _resolve_campaign_id_from_ws(
        workspace, allow_env_fallback=allow_campaign_env_fallback
    )

    missing_reasons: list[str] = []
    if not api_key:
        missing_reasons.append(MISSING_API_KEY)
    if not campaign_id:
        missing_reasons.append(MISSING_CAMPAIGN_ID)

    log.info(
        "instantly_config_resolved",
        extra={
            "workspace_id": workspace_id,
            "workspace_slug": workspace_slug,
            "campaign_id_found": bool(campaign_id),
            "campaign_id_source": campaign_id_source,
            "api_key_found": bool(api_key),
            "api_key_source": api_key_source,
            "api_key_masked": _mask_key(api_key),
        },
    )

    return InstantlyConfig(
        api_key=api_key,
        api_key_source=api_key_source,
        campaign_id=campaign_id,
        campaign_id_source=campaign_id_source,
        missing_reasons=missing_reasons,
    )
