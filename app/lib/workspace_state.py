"""Workspace session-state helpers for the Streamlit UI.

Provides get/set/init wrappers so every page reads workspace identity from a
single source of truth: st.session_state["current_workspace_id"].

All functions that touch st.session_state live here, keeping src/workspace.py
free of Streamlit imports so it can be used safely in scripts and tests.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from src.workspace import (
    get_active_workspaces,
    get_default_workspace_id,
    get_workspace_by_id,
)

_SESSION_KEY = "current_workspace_id"


def init_workspace_state() -> None:
    """Ensure current_workspace_id is set in session state.

    Called once per Streamlit script run (from main.py, before nav.run()).
    If the key is absent or the stored id is no longer a valid active
    workspace, resets to the default OSP workspace id.  Safe to call
    multiple times — subsequent calls are no-ops when the value is valid.
    """
    stored = st.session_state.get(_SESSION_KEY)
    if stored is not None:
        # Validate the stored id is still an active workspace.
        ws = get_workspace_by_id(stored)
        if ws is not None and ws.get("is_active"):
            return  # already valid — nothing to do
    # Either absent or stale — fall back to the default workspace.
    st.session_state[_SESSION_KEY] = get_default_workspace_id()


def get_current_workspace_id() -> int | None:
    """Return the workspace id currently selected for this browser session."""
    return st.session_state.get(_SESSION_KEY)


def get_current_workspace() -> dict[str, Any] | None:
    """Return the full workspace dict for the current selection, or None."""
    ws_id = get_current_workspace_id()
    if ws_id is None:
        return None
    return get_workspace_by_id(ws_id)


def set_current_workspace(workspace_id: int) -> None:
    """Switch the current workspace for this session and mark caches stale."""
    st.session_state[_SESSION_KEY] = workspace_id


def render_workspace_banner() -> None:
    """Render a one-line 'Current workspace: OSP' caption on the active page.

    Intended as a small orientation indicator on pages that operate on
    workspace-scoped data (Leads, Lead Detail, Run Pipeline, Engagement,
    Prompts, Settings).
    """
    ws = get_current_workspace()
    if ws:
        st.caption(f"🏢 **Current workspace:** {ws['name']}  ·  ID {ws['id']}")
    else:
        st.caption(":gray[No workspace selected — data may be unfiltered]")
