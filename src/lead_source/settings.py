"""Per-workspace lead source API settings.

Stored as JSON in workspaces.lead_source_config (same pattern as icp_config).
NULL means the workspace has not configured a lead source yet.

Security notes:
- api_key is stored in the workspace row (same as instantly_api_key).
- mask_api_key() is used everywhere the key is displayed in the UI.
- The raw key MUST NOT appear in logs.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

log = logging.getLogger(__name__)


class LeadSourceConfig(BaseModel):
    enabled: bool = False
    api_base_url: str = ""
    api_key: str = ""          # stored encrypted-at-rest by the DB; masked in UI
    client_slug: str = ""
    daily_fetch_limit: int = 25
    # Optional per-workspace defaults for contact filtering.
    # Passed as query params when fetching contacts; empty string = no filter.
    default_icp: str = ""
    default_status_filter: str = ""
    include_suppressed: bool = False
    # Updated after each import run.
    last_fetched_at: Optional[str] = None
    last_fetch_status: Optional[str] = None
    last_fetch_result_count: Optional[int] = None


def load_lead_source_config(workspace_id: int) -> LeadSourceConfig:
    """Return the lead source config for the given workspace, or defaults."""
    from src.db import session_scope
    from src.models import Workspace
    try:
        with session_scope() as session:
            ws = session.get(Workspace, workspace_id)
            if ws is None or ws.lead_source_config is None:
                return LeadSourceConfig()
            return LeadSourceConfig.model_validate(ws.lead_source_config)
    except Exception as exc:
        log.warning(
            "load_lead_source_config_failed",
            extra={"workspace_id": workspace_id, "error": str(exc)},
        )
        return LeadSourceConfig()


def save_lead_source_config(config: LeadSourceConfig, workspace_id: int) -> None:
    """Persist lead source config for the given workspace."""
    from src.db import session_scope
    from src.models import Workspace
    with session_scope() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            raise ValueError(f"Workspace {workspace_id} not found")
        ws.lead_source_config = config.model_dump()
        log.info(
            "lead_source_config_saved",
            extra={
                "workspace_id": workspace_id,
                "slug": config.client_slug,
                "enabled": config.enabled,
                "key_present": bool(config.api_key),
            },
        )


def update_fetch_metadata(
    workspace_id: int,
    *,
    status: str,
    result_count: int,
    fetched_at: Optional[str] = None,
) -> None:
    """Update last_fetch_* fields after an import run without touching other settings."""
    try:
        cfg = load_lead_source_config(workspace_id)
        cfg.last_fetch_status = status
        cfg.last_fetch_result_count = result_count
        cfg.last_fetched_at = fetched_at or datetime.utcnow().isoformat()
        save_lead_source_config(cfg, workspace_id)
    except Exception as exc:
        log.warning(
            "update_fetch_metadata_failed",
            extra={"workspace_id": workspace_id, "error": str(exc)},
        )


def mask_api_key(key: str) -> str:
    """Return a display-safe masked version showing only the last 4 characters."""
    if not key:
        return ""
    last4 = key[-4:] if len(key) >= 4 else key
    return f"{'•' * 8} {last4}"
