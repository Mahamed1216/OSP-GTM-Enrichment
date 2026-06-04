"""Settings — edit the ICP config that drives scoring, content, and Tavily queries.

Four sections (Company / ICP / Persona / Signals) match the dataclass layout
in src/icp_config.py. Each section is wrapped in a form so partial edits do
not trigger Streamlit reruns. "Save settings" round-trips the values through
the Pydantic model and writes data/icp_config.json atomically. "Reset to
defaults" is a two-click confirmation that reuses the TTL pattern from the
lead-detail delete button.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from app.lib.workspace_state import get_current_workspace, get_current_workspace_id, render_workspace_banner
from app.styles import inject_styles
from src.workspace import (
    get_campaign_id_source,
    get_default_workspace,
    get_workspace_table_stats,
)
from src.icp_config import (
    CONFIG_PATH,
    BuyerPersona,
    CompanyProfile,
    ICPConfig,
    ICPDefinition,
    IntentSignals,
    default_icp_config,
    load_icp_config,
    save_icp_config,
)

inject_styles()

RESET_TTL = 10.0


def _lines(s: str) -> list[str]:
    """Split a text_area value into a list, dropping blank lines."""
    return [ln.strip() for ln in (s or "").splitlines() if ln.strip()]


def _joined(items: list[str]) -> str:
    return "\n".join(items or [])


def _last_saved_caption(path: Path) -> str:
    if not path.exists():
        return ":gray[Config file not yet created — showing defaults.]"
    ts = datetime.fromtimestamp(path.stat().st_mtime)
    return f":gray[Last saved: {ts.strftime('%Y-%m-%d %H:%M:%S')}]"


st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Settings.</h1>'
    '<p class="hero-sublabel">ICP, send rules, demo toggles. Under the hood.</p>'
    '</div>',
    unsafe_allow_html=True,
)
st.write(_last_saved_caption(CONFIG_PATH))

render_workspace_banner()

# ---------- Workspace foundation (read-only, Phase 2 + Phase 3) ----------
# Phase 3: show the currently selected workspace prominently.
_selected_ws = get_current_workspace()
_selected_ws_id = get_current_workspace_id()
if _selected_ws:
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Selected workspace", _selected_ws.get("name") or "—")
    sc2.metric("Workspace ID", str(_selected_ws.get("id") or "—"))
    _sel_campaign_id = _selected_ws.get("instantly_campaign_id") or "—"
    _sel_src = get_campaign_id_source(_selected_ws_id)
    sc3.metric("Campaign ID", _sel_campaign_id)
    sc4.metric("Campaign source", _sel_src)

with st.expander("Workspace foundation (read-only)", expanded=False):
    try:
        _ws = get_default_workspace()
    except Exception as _ws_exc:
        _ws = None
        st.warning(f"Could not load workspace: {_ws_exc}")

    if _ws:
        _campaign_id_source = get_campaign_id_source()
        _campaign_id_display = _ws.get("instantly_campaign_id") or "—"
        st.caption(
            "Phase 2 — workspace_id columns added to all workspace-scoped tables. "
            "Workspace switching is not yet available."
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Name", _ws.get("name") or "—")
        c2.metric("Slug", _ws.get("slug") or "—")
        c3.metric("Default", "Yes" if _ws.get("is_default") else "No")
        c4.metric("Workspace ID", str(_ws.get("id") or "—"))
        st.markdown(
            f"**Instantly campaign ID:** `{_campaign_id_display}`  "
            f"·  **Source:** {_campaign_id_source}"
        )

        # Per-table workspace_id coverage diagnostic.
        st.markdown("**workspace_id coverage by table**")
        try:
            _stats = get_workspace_table_stats()
            _table_rows = []
            for _tname, _tdata in (_stats.get("tables") or {}).items():
                if not isinstance(_tdata, dict) or _tdata.get("status") != "ok":
                    _table_rows.append({
                        "Table": _tname,
                        "Total rows": "—",
                        "Assigned to OSP": "—",
                        "Missing workspace_id": "—",
                        "Status": _tdata.get("status", "—") if isinstance(_tdata, dict) else str(_tdata),
                    })
                else:
                    _table_rows.append({
                        "Table": _tname,
                        "Total rows": _tdata["total_rows"],
                        "Assigned to OSP": _tdata["assigned_to_default"],
                        "Missing workspace_id": _tdata["missing_workspace_id"],
                        "Status": "ok",
                    })
            if _table_rows:
                import pandas as pd
                st.dataframe(pd.DataFrame(_table_rows), hide_index=True, use_container_width=True)
        except Exception as _stats_exc:
            st.caption(f":gray[Table stats unavailable: {_stats_exc}]")
    else:
        st.info(
            "Default workspace not found. The migration runs automatically on "
            "startup — refresh the page if this persists after a few seconds."
        )

cfg = load_icp_config()

# ---------- Company ----------
with st.expander("Company", expanded=True):
    with st.form("settings_company_form", clear_on_submit=False):
        company_name = st.text_input(
            "Company name",
            value=cfg.company.name,
            key="s_company_name",
        )
        company_one_liner = st.text_area(
            "One-liner (what you sell, in one sentence)",
            value=cfg.company.one_liner,
            key="s_company_one_liner",
            height=70,
        )
        company_value_props = st.text_area(
            "Value props (one per line)",
            value=_joined(cfg.company.value_props),
            key="s_company_value_props",
            height=120,
        )
        company_differentiators = st.text_area(
            "Differentiators (one per line)",
            value=_joined(cfg.company.differentiators),
            key="s_company_differentiators",
            height=120,
        )
        save_company = st.form_submit_button("Save company", type="primary")

# ---------- ICP definition ----------
with st.expander("ICP definition", expanded=True):
    with st.form("settings_icp_form", clear_on_submit=False):
        target_industries = st.text_area(
            "Target industries (one per line)",
            value=_joined(cfg.icp.target_industries),
            key="s_icp_industries",
            height=120,
        )
        target_company_sizes = st.text_area(
            "Target company sizes (one per line, e.g. '50-200 employees')",
            value=_joined(cfg.icp.target_company_sizes),
            key="s_icp_sizes",
            height=80,
        )
        target_company_stages = st.text_area(
            "Target company stages (one per line)",
            value=_joined(cfg.icp.target_company_stages),
            key="s_icp_stages",
            height=100,
        )
        target_tech_stack_signals = st.text_area(
            "Target tech-stack signals (one per line)",
            value=_joined(cfg.icp.target_tech_stack_signals),
            key="s_icp_tech",
            height=100,
        )
        target_geographies = st.text_area(
            "Target geographies (one per line)",
            value=_joined(cfg.icp.target_geographies),
            key="s_icp_geos",
            height=80,
        )
        save_icp = st.form_submit_button("Save ICP", type="primary")

# ---------- Buyer persona ----------
with st.expander("Buyer persona", expanded=True):
    with st.form("settings_persona_form", clear_on_submit=False):
        target_titles = st.text_area(
            "Target titles (one per line)",
            value=_joined(cfg.persona.target_titles),
            key="s_persona_titles",
            height=140,
        )
        seniority_levels = st.text_area(
            "Seniority levels (one per line)",
            value=_joined(cfg.persona.seniority_levels),
            key="s_persona_seniority",
            height=80,
        )
        departments = st.text_area(
            "Departments (one per line)",
            value=_joined(cfg.persona.departments),
            key="s_persona_departments",
            height=80,
        )
        top_pain_points = st.text_area(
            "Top pain points (one per line)",
            value=_joined(cfg.persona.top_pain_points),
            key="s_persona_pains",
            height=120,
        )
        common_objections = st.text_area(
            "Common objections (one per line)",
            value=_joined(cfg.persona.common_objections),
            key="s_persona_objections",
            height=120,
        )
        save_persona = st.form_submit_button("Save persona", type="primary")

# ---------- Intent signals ----------
with st.expander("Intent signals", expanded=True):
    with st.form("settings_signals_form", clear_on_submit=False):
        positive_signals = st.text_area(
            "Positive signals — boost score / cite if present (one per line)",
            value=_joined(cfg.signals.positive_signals),
            key="s_signals_positive",
            height=120,
        )
        disqualifiers = st.text_area(
            "Disqualifiers — mark as poor fit, do not target (one per line)",
            value=_joined(cfg.signals.disqualifiers),
            key="s_signals_disqualifiers",
            height=120,
        )
        save_signals = st.form_submit_button("Save signals", type="primary")

# ---------- News search terms ----------
with st.expander("News search terms (Tavily queries)", expanded=False):
    with st.form("settings_news_form", clear_on_submit=False):
        st.caption(
            "Used to qualify Tavily company-news and industry-news searches. "
            "Top two terms anchor company-news queries; the full list drives "
            "the industry-news feed."
        )
        news_search_terms = st.text_area(
            "Search terms (one per line)",
            value=_joined(cfg.news_search_terms),
            key="s_news_terms",
            height=120,
        )
        save_news = st.form_submit_button("Save news terms", type="primary")

# ---------- Demo / testing ----------
with st.expander("Demo / testing", expanded=False):
    with st.form("settings_demo_form", clear_on_submit=False):
        generate_for_all = st.toggle(
            "Generate content for all tiers (testing mode)",
            value=cfg.generate_content_for_all_tiers,
            key="s_demo_all_tiers",
            help=(
                "When ON, the pipeline generates email/call/DM even for "
                "Tier C leads. Delivery is still gated on SEND_MIN_TIER, so "
                "this is a content-preview override only — it will not send."
            ),
        )
        save_demo = st.form_submit_button("Save toggle", type="primary")


# ---------- Save handlers ----------
def _persist(updated: ICPConfig, label: str) -> None:
    try:
        save_icp_config(updated)
    except OSError as exc:
        st.error(f"Failed to save {label}: {exc}")
        return
    st.cache_data.clear()
    st.success(f"{label} saved.")


if save_company:
    cfg.company = CompanyProfile(
        name=company_name.strip() or cfg.company.name,
        one_liner=company_one_liner.strip(),
        value_props=_lines(company_value_props),
        differentiators=_lines(company_differentiators),
    )
    _persist(cfg, "Company")

if save_icp:
    cfg.icp = ICPDefinition(
        target_industries=_lines(target_industries),
        target_company_sizes=_lines(target_company_sizes),
        target_company_stages=_lines(target_company_stages),
        target_tech_stack_signals=_lines(target_tech_stack_signals),
        target_geographies=_lines(target_geographies),
    )
    _persist(cfg, "ICP definition")

if save_persona:
    cfg.persona = BuyerPersona(
        target_titles=_lines(target_titles),
        seniority_levels=_lines(seniority_levels),
        departments=_lines(departments),
        top_pain_points=_lines(top_pain_points),
        common_objections=_lines(common_objections),
    )
    _persist(cfg, "Buyer persona")

if save_signals:
    cfg.signals = IntentSignals(
        positive_signals=_lines(positive_signals),
        disqualifiers=_lines(disqualifiers),
    )
    _persist(cfg, "Intent signals")

if save_news:
    cfg.news_search_terms = _lines(news_search_terms)
    _persist(cfg, "News search terms")

if save_demo:
    cfg.generate_content_for_all_tiers = bool(generate_for_all)
    _persist(cfg, "Demo toggle")

# ---------- Danger zone: reset to defaults ----------
# Two-click TTL pattern mirrors app/pages/3_lead_detail.py:468-533 so the
# UI affordance for destructive actions is consistent across the app.
st.divider()
with st.container(border=True):
    st.markdown(":red[**Danger zone**]")

    pending_key = "icp_reset_pending"
    pending_at_key = "icp_reset_pending_at"

    pending = bool(st.session_state.get(pending_key, False))
    pending_at = float(st.session_state.get(pending_at_key, 0.0))
    if pending and (time.monotonic() - pending_at) > RESET_TTL:
        pending = False
        st.session_state[pending_key] = False
        st.caption(":gray[Previous reset confirmation expired.]")

    if not pending:
        st.caption(
            "Replace every section with the seeded defaults. "
            "Any unsaved edits in the forms above will be lost."
        )
        if st.button(
            "Reset to defaults",
            type="secondary",
            key="icp_reset_init",
        ):
            st.session_state[pending_key] = True
            st.session_state[pending_at_key] = time.monotonic()
            st.rerun()
    else:
        if st.button(
            "⚠️ Click again to confirm reset — overwrites your ICP config",
            type="primary",
            key="icp_reset_confirm",
        ):
            try:
                save_icp_config(default_icp_config())
            except OSError as exc:
                st.error(f"Reset failed: {exc}")
                st.session_state.pop(pending_key, None)
                st.session_state.pop(pending_at_key, None)
            else:
                st.session_state.pop(pending_key, None)
                st.session_state.pop(pending_at_key, None)
                st.cache_data.clear()
                st.success("ICP config reset to defaults.")
                st.rerun()
