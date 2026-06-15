"""Streamlit entry. Run with: streamlit run app/main.py"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env so subprocesses inherit it

import streamlit as st

st.set_page_config(
    page_title="SDR Enablement",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Bridge st.secrets → os.environ BEFORE any src.* import, so pydantic-settings
# (src.config.Settings) picks up Cloud-injected secrets. Also propagates them
# to any subprocess spawned later (scripts/run_pipeline.py etc).
from app.lib.config import bootstrap_env_from_streamlit_secrets  # noqa: E402
bootstrap_env_from_streamlit_secrets()

from app.lib.auth import (  # noqa: E402  after bootstrap
    is_auth_enabled,
    is_auth_misconfigured,
    is_authenticated,
    login,
    logout,
)
from app.styles import inject_styles  # noqa: E402  after set_page_config

inject_styles()

# -----------------------------------------------------------------------
# Auth gate (fail-closed). Authentication is required by default; set
# APP_PASSWORD to enable login. Auth can only be disabled by explicitly
# setting AUTH_REQUIRED=false (local-dev opt-in). If auth is required but
# no password is configured, refuse to render rather than expose the app.
# -----------------------------------------------------------------------
if is_auth_misconfigured():
    st.title("Configuration required")
    st.error(
        "This deployment requires authentication but no access password is "
        "set. Configure **APP_PASSWORD** (in Streamlit secrets or the "
        "environment) to enable sign-in.\n\n"
        "For local development only, you may set **AUTH_REQUIRED=false** to "
        "run without a password."
    )
    st.stop()

if is_auth_enabled() and not is_authenticated():
    st.title("Sign in to OSP")
    st.caption("Enter the access password to continue.")
    with st.form("login_form", clear_on_submit=False):
        password = st.text_input("Password", type="password", key="login_password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if login(password):
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

# setup_logging is guarded by session state (calling it twice in a session
# is harmless, but we skip the overhead when already done for this session).
if not st.session_state.get("_app_initialized"):
    from src.logging_setup import setup_logging
    setup_logging()
    st.session_state["_app_initialized"] = True

# init_db() is guarded by a *process-level* flag inside the function, so
# calling it here on every Streamlit script execution is a fast no-op after
# the first successful run.  This is intentional: st.session_state can
# survive a Streamlit Cloud server restart (the browser reconnects and
# rehydrates session state), which would cause the old session-only guard to
# skip init_db() on a fresh process — leaving the workspaces table and
# workspace_id columns uncreated in production Postgres.
from src.db import init_db
init_db()

# Phase 3: ensure session state has a valid current workspace selection.
from app.lib.workspace_state import (  # noqa: E402  after init_db
    get_current_workspace_id,
    get_current_workspace,
    init_workspace_state,
    set_current_workspace,
)
from src.workspace import get_active_workspaces
init_workspace_state()

# -----------------------------------------------------------------------
# Sidebar — workspace selector (bottom-left, above sign-out)
# -----------------------------------------------------------------------
with st.sidebar:
    # Workspace selector — profile-switcher style.
    try:
        _ws_list = get_active_workspaces()
    except Exception:
        _ws_list = []

    if _ws_list:
        _ws_ids = [ws["id"] for ws in _ws_list]
        _ws_names = {ws["id"]: ws["name"] for ws in _ws_list}
        _cur_ws_id = get_current_workspace_id()
        _default_idx = _ws_ids.index(_cur_ws_id) if _cur_ws_id in _ws_ids else 0

        st.markdown("**Workspace**")
        _selected_ws_id = st.selectbox(
            "workspace_select",
            options=_ws_ids,
            format_func=lambda wid: _ws_names.get(wid, f"#{wid}"),
            index=_default_idx,
            key="sidebar_ws_selector",
            label_visibility="collapsed",
            help="Switch the active workspace. All pages scope data to this selection.",
        )
        if _selected_ws_id is not None and _selected_ws_id != _cur_ws_id:
            set_current_workspace(_selected_ws_id)
            st.rerun()
    else:
        st.caption(":gray[No workspaces found]")

    st.divider()

    if is_auth_enabled():
        if st.button("Sign out", use_container_width=True, key="sidebar_signout"):
            logout()
            st.rerun()
    else:
        st.caption(":orange[Dev mode — auth disabled]")

_PAGES_DIR = Path(__file__).parent / "pages"

dashboard = st.Page(
    str(_PAGES_DIR / "1_dashboard.py"),
    title="Dashboard",
    icon="📊",
    default=True,
    url_path="dashboard",
)
leads = st.Page(
    str(_PAGES_DIR / "2_leads.py"),
    title="Leads",
    icon="👥",
    url_path="leads",
)
lead_detail = st.Page(
    str(_PAGES_DIR / "3_lead_detail.py"),
    title="Lead Detail",
    icon="🔍",
    url_path="lead-detail",
)
run_pipeline = st.Page(
    str(_PAGES_DIR / "4_run_pipeline.py"),
    title="Run Pipeline",
    icon="⚙️",
    url_path="run-pipeline",
)
settings_page = st.Page(
    str(_PAGES_DIR / "6_settings.py"),
    title="Settings",
    icon="🎯",
    url_path="settings",
)
engagement = st.Page(
    str(_PAGES_DIR / "5_engagement.py"),
    title="Engagement",
    icon="📬",
    url_path="engagement",
)
lead_sources_page = st.Page(
    str(_PAGES_DIR / "8_lead_sources.py"),
    title="Lead Sources",
    icon="🌐",
    url_path="lead-sources",
)
prompts_page = st.Page(
    str(_PAGES_DIR / "7_prompts.py"),
    title="Prompts",
    icon="📝",
    url_path="prompts",
)

nav = st.navigation([
    dashboard, leads, lead_detail, run_pipeline,
    lead_sources_page,
    engagement, settings_page, prompts_page,
])
nav.run()
