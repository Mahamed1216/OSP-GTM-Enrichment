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
from datetime import datetime, timezone
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
    default_icp: str = ""
    default_status_filter: str = ""
    include_suppressed: bool = False
    # Phase 8: evergreen automation settings.
    # auto_import_enabled: pull new contacts on each scheduled run.
    # auto_process_enabled: score + generate content for newly imported leads.
    # schedule_frequency: hint for the operator — actual scheduling is external
    #   (Render Cron / GitHub Actions calling the CLI or webhook endpoint).
    auto_import_enabled: bool = False
    auto_process_enabled: bool = False
    schedule_frequency: str = "daily"   # "manual" | "hourly" | "daily"
    last_auto_run_at: Optional[str] = None
    last_auto_run_status: Optional[str] = None
    last_auto_run_created: Optional[int] = None
    last_auto_run_scored: Optional[int] = None
    last_auto_run_content: Optional[int] = None
    last_auto_run_fetched: Optional[int] = None   # total API contacts fetched last run
    last_auto_run_skipped: Optional[int] = None   # dedup-skipped last run
    # Updated after each manual or auto import run.
    last_fetched_at: Optional[str] = None
    last_fetch_status: Optional[str] = None
    last_fetch_result_count: Optional[int] = None
    # Evergreen pagination cursor.  Scheduled imports start at this offset and
    # advance by fetched_count each run.  Resets to 0 when the end of the
    # remote list is reached (fetched_count < limit) so the next cycle picks
    # up newly added contacts from the front.  Manual imports always use 0.
    next_offset: int = 0


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
        cfg.last_fetched_at = fetched_at or datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        save_lead_source_config(cfg, workspace_id)
    except Exception as exc:
        log.warning(
            "update_fetch_metadata_failed",
            extra={"workspace_id": workspace_id, "error": str(exc)},
        )


def update_auto_run_metadata(
    workspace_id: int,
    *,
    status: str,
    created: int = 0,
    scored: int = 0,
    content: int = 0,
    fetched: int = 0,
    skipped: int = 0,
    ran_at: Optional[str] = None,
) -> None:
    """Persist last_auto_run_* fields after an evergreen scheduler run."""
    try:
        cfg = load_lead_source_config(workspace_id)
        cfg.last_auto_run_at = ran_at or datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        cfg.last_auto_run_status = status
        cfg.last_auto_run_created = created
        cfg.last_auto_run_scored = scored
        cfg.last_auto_run_content = content
        cfg.last_auto_run_fetched = fetched
        cfg.last_auto_run_skipped = skipped
        save_lead_source_config(cfg, workspace_id)
    except Exception as exc:
        log.warning(
            "update_auto_run_metadata_failed",
            extra={"workspace_id": workspace_id, "error": str(exc)},
        )


def mask_api_key(key: str) -> str:
    """Return a display-safe masked version showing only the last 4 characters."""
    if not key:
        return ""
    last4 = key[-4:] if len(key) >= 4 else key
    return f"{'•' * 8} {last4}"


def advance_import_cursor(
    workspace_id: int,
    *,
    fetched_count: int,
    limit: int,
) -> int:
    """Advance next_offset after a scheduled run and persist it.

    Strategy: rolling offset with end-of-results wrap-around.
    - fetched_count == limit  →  more pages remain; advance by fetched_count.
    - fetched_count < limit   →  reached the end of the remote list; reset to 0
      so the next cycle starts from the front (where new contacts appear).

    Returns the new next_offset value.
    """
    try:
        cfg = load_lead_source_config(workspace_id)
        old_offset = cfg.next_offset
        if fetched_count < limit:
            new_offset = 0  # end of list reached — wrap around
        else:
            new_offset = old_offset + fetched_count
        cfg.next_offset = new_offset
        save_lead_source_config(cfg, workspace_id)
        log.info(
            "import_cursor_advanced",
            extra={
                "workspace_id": workspace_id,
                "old_offset": old_offset,
                "new_offset": new_offset,
                "fetched": fetched_count,
                "limit": limit,
                "wrapped": new_offset == 0 and fetched_count < limit,
            },
        )
        return new_offset
    except Exception as exc:
        log.warning(
            "advance_import_cursor_failed",
            extra={"workspace_id": workspace_id, "error": str(exc)},
        )
        return 0


def reset_import_cursor(workspace_id: int) -> None:
    """Reset next_offset to 0 so the next scheduled run starts from the beginning."""
    try:
        cfg = load_lead_source_config(workspace_id)
        cfg.next_offset = 0
        save_lead_source_config(cfg, workspace_id)
        log.info("import_cursor_reset", extra={"workspace_id": workspace_id})
    except Exception as exc:
        log.warning(
            "reset_import_cursor_failed",
            extra={"workspace_id": workspace_id, "error": str(exc)},
        )
