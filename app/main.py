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
    is_authenticated,
    login,
    logout,
)
from app.styles import inject_styles  # noqa: E402  after set_page_config

inject_styles()

# -----------------------------------------------------------------------
# Auth gate. Shared password from APP_PASSWORD. If unset, dev-mode bypass.
# -----------------------------------------------------------------------
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

with st.sidebar:
    if is_auth_enabled():
        if st.button("Sign out", use_container_width=True, key="sidebar_signout"):
            logout()
            st.rerun()
    else:
        st.caption(":orange[Dev mode — auth disabled]")

if not st.session_state.get("_app_initialized"):
    from src.db import init_db
    from src.logging_setup import setup_logging

    setup_logging()
    init_db()
    st.session_state["_app_initialized"] = True

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
prompts_page = st.Page(
    str(_PAGES_DIR / "7_prompts.py"),
    title="Prompts",
    icon="📝",
    url_path="prompts",
)

nav = st.navigation([dashboard, leads, lead_detail, run_pipeline, settings_page, engagement, prompts_page])
nav.run()
