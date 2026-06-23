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

from app.lib.workspace_state import get_current_workspace, get_current_workspace_id, render_workspace_banner, set_current_workspace
from app.styles import inject_styles
from src.delivery.instantly_config import build_instantly_diagnostic, resolve_instantly_config
from src.workspace import (
    backfill_osp_icp_config,
    create_workspace,
    get_api_key_source,
    get_campaign_id_source,
    get_default_workspace,
    get_default_workspace_id,
    get_workspace_table_stats,
    restore_osp_from_legacy_config,
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
    load_workspace_icp_config,
    save_icp_config,
    save_workspace_icp_config,
)

inject_styles()

RESET_TTL = 10.0


def _lines(s: str) -> list[str]:
    """Split a text_area value into a list, dropping blank lines."""
    return [ln.strip() for ln in (s or "").splitlines() if ln.strip()]


def _joined(items: list[str]) -> str:
    return "\n".join(items or [])


def _last_saved_caption(path: Path, workspace_id: int | None = None) -> str:
    # Check if the workspace has DB-stored settings (takes precedence over file).
    if workspace_id is not None:
        try:
            from src.db import session_scope
            from src.models import Workspace
            with session_scope() as _s:
                _ws = _s.get(Workspace, workspace_id)
                if _ws is not None and _ws.icp_config is not None:
                    ts_str = str(_ws.updated_at)[:19] if _ws.updated_at else "—"
                    return f":gray[Last saved: {ts_str}]"
        except Exception:
            pass
    if not path.exists():
        return ":gray[Settings not yet saved for this workspace — showing defaults.]"
    ts = datetime.fromtimestamp(path.stat().st_mtime)
    return f":gray[Last saved: {ts.strftime('%Y-%m-%d %H:%M:%S')} (file)]"


st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Settings.</h1>'
    '<p class="hero-sublabel">ICP, send rules, demo toggles. Under the hood.</p>'
    '</div>',
    unsafe_allow_html=True,
)
st.write(_last_saved_caption(CONFIG_PATH, workspace_id=get_current_workspace_id()))

render_workspace_banner()

# ---------- Workspace foundation (read-only, Phase 2 + Phase 3) ----------
# Phase 3: show the currently selected workspace prominently.
_selected_ws = get_current_workspace()
_selected_ws_id = get_current_workspace_id()

# ---------------------------------------------------------------------------
# Workspace-change guard
#
# Streamlit form widgets (st.text_input, st.text_area, st.toggle) cache their
# displayed value in st.session_state under a fixed key like "s_company_name".
# When the user switches to a different workspace, the script reruns with a
# new _selected_ws_id but the same session_state — so every form field still
# shows the *previous* workspace's value.  To an observer this looks exactly
# like a settings leak across workspaces, even though the DB is correct.
#
# Fix: whenever _selected_ws_id changes, evict all form widget keys.  The
# widgets then fall back to their value= parameter, which reads from the fresh
# load_workspace_icp_config() call below, showing the correct workspace data.
# ---------------------------------------------------------------------------
_SETTINGS_FORM_KEYS = (
    "s_company_name", "s_company_one_liner", "s_company_value_props",
    "s_company_differentiators",
    "s_icp_industries", "s_icp_sizes", "s_icp_stages", "s_icp_tech", "s_icp_geos",
    "s_persona_titles", "s_persona_seniority", "s_persona_departments",
    "s_persona_pains", "s_persona_objections",
    "s_signals_positive", "s_signals_disqualifiers",
    "s_news_terms", "s_demo_all_tiers",
)
_SETTINGS_LAST_WS_KEY = "settings_last_workspace_id"
if st.session_state.get(_SETTINGS_LAST_WS_KEY) != _selected_ws_id:
    for _fk in _SETTINGS_FORM_KEYS:
        st.session_state.pop(_fk, None)
    st.session_state[_SETTINGS_LAST_WS_KEY] = _selected_ws_id

if _selected_ws:
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Selected workspace", _selected_ws.get("name") or "—")
    sc2.metric("Workspace ID", str(_selected_ws.get("id") or "—"))
    _sel_campaign_id = _selected_ws.get("instantly_campaign_id") or "—"
    _sel_src = get_campaign_id_source(_selected_ws_id)
    sc3.metric("Campaign ID", _sel_campaign_id)
    sc4.metric("Campaign source", _sel_src)
    # Second row: additional workspace details
    sd1, sd2, sd3, sd4 = st.columns(4)
    sd1.metric("Slug", _selected_ws.get("slug") or "—")
    _api_key_src = get_api_key_source(_selected_ws_id)
    sd2.metric("API key source", _api_key_src)
    sd3.metric("Active", "Yes" if _selected_ws.get("is_active") else "No")
    sd4.metric("Default", "Yes" if _selected_ws.get("is_default") else "No")

    # --- Instantly push configuration (what the Push-to-Instantly flow uses) ---
    # API key = shared env/Streamlit secret (never per-workspace); campaign id =
    # this workspace's value (env fallback only when no workspace is selected).
    _inst_cfg = resolve_instantly_config(
        _selected_ws_id, allow_campaign_env_fallback=(_selected_ws_id is None)
    )
    _API_SRC_LABELS = {
        "env": "Environment (INSTANTLY_API_KEY)",
        "streamlit_secrets": "Streamlit secrets",
        "config": "App config (.env)",
        "missing": "Missing",
    }
    _CAMP_SRC_LABELS = {
        "workspace_column": "Workspace",
        "workspace_config": "Workspace config",
        "env": "Environment (INSTANTLY_CAMPAIGN_ID)",
        "streamlit_secrets": "Streamlit secrets",
        "missing": "Missing",
    }
    st.markdown("**Instantly push configuration**")
    ic1, ic2, ic3, ic4 = st.columns(4)
    ic1.metric("API key configured", "Yes" if _inst_cfg.api_key else "No")
    ic2.metric("API key source", _API_SRC_LABELS.get(_inst_cfg.api_key_source, _inst_cfg.api_key_source))
    ic3.metric("Campaign ID configured", "Yes" if _inst_cfg.campaign_id else "No")
    ic4.metric("Campaign ID source", _CAMP_SRC_LABELS.get(_inst_cfg.campaign_id_source, _inst_cfg.campaign_id_source))
    if _inst_cfg.missing_reasons:
        st.warning(
            "Instantly push not ready — " + ", ".join(_inst_cfg.missing_reasons)
            + ".  API key must be set via the INSTANTLY_API_KEY environment "
            "variable or Streamlit secret; campaign ID is set per workspace."
        )
    else:
        st.caption(
            "Instantly push is configured: API key from "
            f"{_API_SRC_LABELS.get(_inst_cfg.api_key_source, _inst_cfg.api_key_source)}, "
            f"campaign ID from {_CAMP_SRC_LABELS.get(_inst_cfg.campaign_id_source, _inst_cfg.campaign_id_source)}."
        )

    # --- Temporary runtime diagnostic: which runtime/source exposes creds? ---
    # Booleans + masked prefix only — the full API key is never shown/logged.
    with st.expander("Instantly runtime diagnostic (temporary)", expanded=bool(_inst_cfg.missing_reasons)):
        _diag = build_instantly_diagnostic(
            _selected_ws_id, allow_campaign_env_fallback=(_selected_ws_id is None)
        )
        dg1, dg2 = st.columns(2)
        with dg1:
            st.markdown(f"**workspace_id:** `{_diag['workspace_id']}`")
            st.markdown(f"**workspace_slug:** `{_diag['workspace_slug']}`")
            st.markdown(f"**api_key_found:** {'yes' if _diag['api_key_found'] else 'no'}")
            st.markdown(f"**api_key_source:** `{_diag['api_key_source']}`")
            st.markdown(f"**api_key (masked):** `{_diag['api_key_masked']}`")
        with dg2:
            st.markdown(f"**campaign_id_found:** {'yes' if _diag['campaign_id_found'] else 'no'}")
            st.markdown(f"**campaign_id_source:** `{_diag['campaign_id_source']}`")
            st.markdown(
                "**missing_reasons:** "
                + (", ".join(_diag["missing_reasons"]) if _diag["missing_reasons"] else "none")
            )
        st.markdown("**Per-source probes** (presence only — values never shown):")
        st.json(_diag["probes"])

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

# ---------- Workspace management (Phase 4) ----------
with st.expander("Workspace management", expanded=False):
    st.markdown("**Create a new workspace**")
    st.caption(
        "Each workspace is fully isolated: separate leads, prompts, engagement "
        "data, and Instantly campaign. OSP data is never affected."
    )

    with st.form("create_workspace_form", clear_on_submit=True):
        _new_name = st.text_input(
            "Workspace name *",
            placeholder="Client A",
            key="ws_new_name",
            help="Required. Human-readable name shown in the workspace selector.",
        )
        _new_slug = st.text_input(
            "Workspace slug *",
            placeholder="client-a",
            key="ws_new_slug",
            help="Required. Lowercase letters, digits, hyphens, underscores. Must be unique.",
        )
        _new_campaign_id = st.text_input(
            "Instantly campaign ID *",
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            key="ws_new_campaign_id",
            help="Required. Used for engagement sync for this workspace.",
        )
        _new_api_key = st.text_input(
            "Instantly API key (optional — not used for Push)",
            placeholder="Leave blank — Push uses INSTANTLY_API_KEY from env/secrets",
            key="ws_new_api_key",
            type="password",
            help=(
                "Optional and NOT required. Push to Instantly always uses the "
                "INSTANTLY_API_KEY environment variable / Streamlit secret, never "
                "a per-workspace key. This field is retained only for other flows."
            ),
        )
        _new_notes = st.text_area(
            "Notes (optional)",
            key="ws_new_notes",
            height=70,
            placeholder="Client name, account owner, campaign details…",
        )
        _copy_settings = st.checkbox(
            "Copy settings from current workspace",
            value=False,
            key="ws_copy_settings",
            help=(
                "Copies company, ICP, persona, and signal settings from the currently "
                "selected workspace. If unchecked, the new workspace starts with blank settings."
            ),
        )
        _copy_prompts = st.checkbox(
            "Copy prompts from current workspace",
            value=True,
            key="ws_copy_prompts",
            help=(
                "Copies email, LinkedIn DM, and call-script prompts from the currently "
                "selected workspace. Edits to the new workspace's prompts will NOT affect "
                "the source workspace."
            ),
        )
        _copy_winners = st.checkbox(
            "Copy seed winners from current workspace",
            value=False,
            key="ws_copy_winners",
            help=(
                "Copies seed examples (winner_reason=seed) from the winners library. "
                "Engagement winners, manual ratings, and positive-reply winners are NOT copied."
            ),
        )
        _create_btn = st.form_submit_button("Create workspace", type="primary")

    if _create_btn:
        _src_ws_id = _selected_ws_id  # the currently selected workspace
        _copy_settings_from = _src_ws_id if _copy_settings else None
        _copy_prompts_from = _src_ws_id if _copy_prompts else None
        _copy_winners_from = _src_ws_id if _copy_winners else None
        try:
            _new_ws = create_workspace(
                name=_new_name,
                slug=_new_slug,
                instantly_campaign_id=_new_campaign_id,
                instantly_api_key=_new_api_key or None,
                notes=_new_notes or None,
                copy_settings_from_workspace_id=_copy_settings_from,
                copy_prompts_from_workspace_id=_copy_prompts_from,
                copy_seed_winners_from_workspace_id=_copy_winners_from,
            )
            st.success(
                f"Workspace **{_new_ws['name']}** created (ID {_new_ws['id']}). "
                "Switching to it now…"
            )
            set_current_workspace(_new_ws["id"])
            st.cache_data.clear()
            st.rerun()
        except ValueError as _ve:
            st.error(str(_ve))
        except Exception as _exc:
            st.error(f"Workspace creation failed: {_exc}")

    # Show existing workspaces in a compact table
    st.divider()
    st.markdown("**Existing workspaces**")
    try:
        import pandas as _pd
        from src.workspace import get_active_workspaces as _gaw
        _all_ws = _gaw()
        if _all_ws:
            _ws_rows = [
                {
                    "Name": ws.get("name") or "—",
                    "Slug": ws.get("slug") or "—",
                    "ID": ws.get("id"),
                    "Default": "Yes" if ws.get("is_default") else "No",
                    "Campaign ID": ws.get("instantly_campaign_id") or "—",
                    "Created": str(ws.get("created_at") or "—")[:10],
                }
                for ws in _all_ws
            ]
            st.dataframe(_pd.DataFrame(_ws_rows), hide_index=True, use_container_width=True)
        else:
            st.caption(":gray[No workspaces found.]")
    except Exception as _exc:
        st.caption(f":gray[Could not load workspace list: {_exc}]")

cfg = load_workspace_icp_config(workspace_id=_selected_ws_id)

_ws_caption = f"**{_selected_ws['name']}** · ID {_selected_ws['id']}" if _selected_ws else "selected workspace"
st.info(
    f"These settings apply only to the current workspace: {_ws_caption}. "
    "Switching workspaces loads that workspace's own saved settings."
)

# ---------- Autofill settings from website ----------
_af_result_key  = f"_autofill_result_{_selected_ws_id}"
_af_url_key     = f"_autofill_url_{_selected_ws_id}"
_af_confirm_key = f"_autofill_confirm_{_selected_ws_id}"

# Maps each autofill suggestion field to the Streamlit session-state key used
# by the corresponding Settings form widget.  Clearing a key here forces the
# widget to re-initialise from the fresh DB value on the next rerun.
_AUTOFILL_TO_FORM_KEY: dict[str, str] = {
    "company_name":               "s_company_name",
    "one_liner":                  "s_company_one_liner",
    "value_props":                "s_company_value_props",
    "differentiators":            "s_company_differentiators",
    "target_industries":          "s_icp_industries",
    "target_company_sizes":       "s_icp_sizes",
    "target_company_stages":      "s_icp_stages",
    "target_tech_stack_signals":  "s_icp_tech",
    "target_geographies":         "s_icp_geos",
    "target_titles":              "s_persona_titles",
    "seniority_levels":           "s_persona_seniority",
    "departments":                "s_persona_departments",
    "top_pain_points":            "s_persona_pains",
    "common_objections":          "s_persona_objections",
    "positive_signals":           "s_signals_positive",
    "disqualifiers":              "s_signals_disqualifiers",
    "news_search_terms":          "s_news_terms",
}

with st.expander("Autofill settings from website", expanded=False):
    st.caption(
        "Paste a company website URL. The app will research the site, draft "
        "suggested settings, and let you review each field before saving."
    )

    # ---- Persistent confirmation banner (shown after a successful apply) ----
    _af_confirm = st.session_state.get(_af_confirm_key)
    if _af_confirm:
        st.success(
            f"Settings saved for workspace: **{_af_confirm['ws_name']}** "
            f"(ID {_af_confirm['ws_id']})"
        )
        with st.expander("What was saved", expanded=True):
            for _cf_key, _cf_label, _cf_old, _cf_new in _af_confirm["fields"]:
                st.markdown(f"**{_cf_label}**")
                _cc1, _cc2 = st.columns(2)
                with _cc1:
                    st.caption(":gray[Before]")
                    st.code((_cf_old or "(empty)")[:300], language=None)
                with _cc2:
                    st.caption(":gray[After]")
                    st.code((_cf_new or "(empty)")[:300], language=None)
        if st.button("Dismiss", key=f"af_dismiss_{_selected_ws_id}", type="secondary"):
            st.session_state.pop(_af_confirm_key, None)
            st.rerun()
        st.divider()

    # ---- URL input + Analyze button ----
    _af_url = st.text_input(
        "Website URL",
        placeholder="https://company.com",
        key=f"af_url_{_selected_ws_id}",
    )
    _af_notes = st.text_area(
        "Optional notes about the client",
        height=60,
        key=f"af_notes_{_selected_ws_id}",
        placeholder="e.g. B2B SaaS tool, sells to enterprise sales teams in the US",
    )

    _af_col1, _af_col2 = st.columns([2, 1])
    with _af_col1:
        _af_analyze = st.button(
            "Analyze website",
            key=f"af_analyze_{_selected_ws_id}",
            type="primary",
        )
    with _af_col2:
        if st.session_state.get(_af_result_key):
            if st.button("Clear results", key=f"af_clear_{_selected_ws_id}", type="secondary"):
                st.session_state.pop(_af_result_key, None)
                st.session_state.pop(_af_url_key, None)
                st.rerun()

    if _af_analyze:
        if not (_af_url or "").strip():
            st.warning("Please enter a website URL.")
        else:
            with st.spinner("Fetching and analyzing website…"):
                try:
                    from src.website_analyzer import analyze_website as _analyze_website
                    _af_res = _analyze_website(_af_url.strip(), notes=_af_notes or "")
                    st.session_state[_af_result_key] = _af_res
                    st.session_state[_af_url_key] = _af_url.strip()
                    # Clear any stale confirmation when starting a new analysis
                    st.session_state.pop(_af_confirm_key, None)
                except Exception as _af_exc:
                    st.error(f"Analysis failed: {_af_exc}")
                    st.session_state.pop(_af_result_key, None)

    _af_result = st.session_state.get(_af_result_key)

    if _af_result is not None:
        if _af_result.error:
            st.error(_af_result.error)
            if _af_result.debug_info:
                with st.expander("Debug info", expanded=False):
                    st.code(_af_result.debug_info, language=None)
        else:
            _analyzed_url = st.session_state.get(_af_url_key, "")
            _conf_colors = {"high": ":green", "medium": ":orange", "low": ":red"}
            _conf_tag = _af_result.confidence or "medium"
            _conf_display = f"{_conf_colors.get(_conf_tag, ':gray')}[{_conf_tag.title()}]"
            st.success(f"Website analyzed: {_analyzed_url}")
            if _af_result.sources_used:
                st.caption("Sources: " + "  ·  ".join(_af_result.sources_used[:3]))
            st.caption(f"Confidence: {_conf_display}")

            st.divider()
            st.markdown("**Review suggested settings** — uncheck any field to keep your current value:")
            _ws_display_name = _selected_ws.get("name") if _selected_ws else str(_selected_ws_id)
            st.warning(
                f"Applying these settings only updates the current workspace: **{_ws_display_name}**"
            )

            _s = _af_result.suggestions

            def _fmt_val(v) -> str:
                if isinstance(v, list):
                    return "\n".join(str(x) for x in v) if v else ""
                return str(v) if v else ""

            _AF_FIELDS: list[tuple[str, str, str, str]] = [
                ("company_name",             "Company name",       _fmt_val(cfg.company.name),                   _fmt_val(_s.company_name)),
                ("one_liner",                "One-liner",          _fmt_val(cfg.company.one_liner),              _fmt_val(_s.one_liner)),
                ("value_props",              "Value props",        _fmt_val(cfg.company.value_props),            _fmt_val(_s.value_props)),
                ("differentiators",          "Differentiators",    _fmt_val(cfg.company.differentiators),        _fmt_val(_s.differentiators)),
                ("target_industries",        "Target industries",  _fmt_val(cfg.icp.target_industries),          _fmt_val(_s.target_industries)),
                ("target_company_sizes",     "Company sizes",      _fmt_val(cfg.icp.target_company_sizes),       _fmt_val(_s.target_company_sizes)),
                ("target_company_stages",    "Company stages",     _fmt_val(cfg.icp.target_company_stages),      _fmt_val(_s.target_company_stages)),
                ("target_tech_stack_signals","Tech stack signals", _fmt_val(cfg.icp.target_tech_stack_signals),  _fmt_val(_s.target_tech_stack_signals)),
                ("target_geographies",       "Geographies",        _fmt_val(cfg.icp.target_geographies),         _fmt_val(_s.target_geographies)),
                ("target_titles",            "Target titles",      _fmt_val(cfg.persona.target_titles),          _fmt_val(_s.target_titles)),
                ("seniority_levels",         "Seniority levels",   _fmt_val(cfg.persona.seniority_levels),       _fmt_val(_s.seniority_levels)),
                ("departments",              "Departments",        _fmt_val(cfg.persona.departments),            _fmt_val(_s.departments)),
                ("top_pain_points",          "Pain points",        _fmt_val(cfg.persona.top_pain_points),        _fmt_val(_s.top_pain_points)),
                ("common_objections",        "Objections",         _fmt_val(cfg.persona.common_objections),      _fmt_val(_s.common_objections)),
                ("positive_signals",         "Positive signals",   _fmt_val(cfg.signals.positive_signals),       _fmt_val(_s.positive_signals)),
                ("disqualifiers",            "Disqualifiers",      _fmt_val(cfg.signals.disqualifiers),          _fmt_val(_s.disqualifiers)),
                ("news_search_terms",        "News search terms",  _fmt_val(cfg.news_search_terms),              _fmt_val(_s.news_search_terms)),
            ]

            _af_checked: dict[str, bool] = {}
            for _fkey, _flabel, _fcurrent, _fsuggested in _AF_FIELDS:
                if not _fsuggested.strip():
                    continue
                _af_checked[_fkey] = st.checkbox(
                    _flabel,
                    value=True,
                    key=f"af_check_{_selected_ws_id}_{_fkey}",
                )
                _c1, _c2 = st.columns(2)
                with _c1:
                    st.caption(":gray[Current value]")
                    st.code(_fcurrent or "(empty)", language=None)
                with _c2:
                    st.caption(":gray[Suggested value]")
                    st.code(_fsuggested, language=None)
                if _af_result.reasoning.get(_fkey):
                    st.caption(f":gray[{_af_result.reasoning[_fkey]}]")

            st.divider()
            if st.button(
                "Apply suggested settings",
                key=f"af_apply_{_selected_ws_id}",
                type="primary",
            ):
                _to_apply = {k for k, v in _af_checked.items() if v}
                if not _to_apply:
                    st.warning("No fields selected — nothing to apply.")
                elif _selected_ws_id is None:
                    st.error("No workspace selected — cannot save.")
                else:
                    from src.website_analyzer import apply_suggestions_to_config as _apply_sugg
                    _updated_cfg = _apply_sugg(cfg, _s, _to_apply)
                    try:
                        # ---- pre-save snapshot (other workspaces only) ----
                        import json as _json_iso
                        _ws_other_before: dict[int, str] = {}
                        try:
                            from src.db import session_scope as _isess
                            from src.models import Workspace as _IsoWS
                            with _isess() as _si:
                                for _iw in _si.query(_IsoWS).filter(
                                    _IsoWS.id != _selected_ws_id
                                ).all():
                                    _ws_other_before[_iw.id] = _json_iso.dumps(
                                        _iw.icp_config or {}, sort_keys=True
                                    )
                        except Exception:
                            pass

                        save_workspace_icp_config(_updated_cfg, workspace_id=_selected_ws_id)
                        st.cache_data.clear()

                        # ---- post-save isolation check ----
                        _breach_ids: list[int] = []
                        if _ws_other_before:
                            try:
                                from src.db import session_scope as _isess2
                                from src.models import Workspace as _IsoWS2
                                with _isess2() as _si2:
                                    for _iw2 in _si2.query(_IsoWS2).filter(
                                        _IsoWS2.id != _selected_ws_id
                                    ).all():
                                        _after = _json_iso.dumps(
                                            _iw2.icp_config or {}, sort_keys=True
                                        )
                                        if _after != _ws_other_before.get(_iw2.id, _after):
                                            _breach_ids.append(_iw2.id)
                            except Exception:
                                pass

                        if _breach_ids:
                            st.error(
                                f"WARNING: workspace isolation breach — workspace IDs "
                                f"{_breach_ids} were unexpectedly modified. "
                                "Do not use these workspaces until this is resolved."
                            )
                        else:
                            # Save confirmed clean — commit confirmation + clear form cache
                            _ws_name = _selected_ws.get("name") if _selected_ws else str(_selected_ws_id)
                            _confirm_fields = [
                                (fkey, flabel, fcurrent, fsuggested)
                                for fkey, flabel, fcurrent, fsuggested in _AF_FIELDS
                                if fkey in _to_apply
                            ]
                            st.session_state[_af_confirm_key] = {
                                "ws_id":   _selected_ws_id,
                                "ws_name": _ws_name,
                                "fields":  _confirm_fields,
                            }

                            # Clear form widget session-state keys for updated fields
                            # so the Settings forms re-initialise from fresh DB values.
                            for _fk in _to_apply:
                                _form_sk = _AUTOFILL_TO_FORM_KEY.get(_fk)
                                if _form_sk and _form_sk in st.session_state:
                                    del st.session_state[_form_sk]

                            st.session_state.pop(_af_result_key, None)
                            st.session_state.pop(_af_url_key, None)
                            st.rerun()
                    except Exception as _save_exc:
                        st.error(f"Failed to save settings: {_save_exc}")

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

# ---------- Buyer research ----------
with st.expander("Buyer research", expanded=False):
    with st.form("settings_buyer_research_form", clear_on_submit=False):
        buyer_news_window = st.number_input(
            "Buyer research news window days",
            min_value=1,
            max_value=365,
            value=int(cfg.buyer_research_news_window_days or 90),
            step=15,
            key="s_buyer_news_window",
            help=(
                "Buyer research will search for relevant company signals within "
                "this many days. Higher values may find more relevant but less "
                "fresh signals."
            ),
        )
        st.caption("Crawl and extract improve research quality but may use more Tavily credits.")
        buyer_use_research = st.checkbox(
            "Use Tavily Research for company research",
            value=cfg.buyer_research_use_research,
            key="s_buyer_use_research",
            help=(
                "Uses Tavily's research agent for deeper company research. "
                "This is expensive. Leave off unless needed."
            ),
        )
        buyer_use_crawl = st.checkbox(
            "Use Tavily company crawl for buyer research",
            value=cfg.buyer_research_use_crawl,
            key="s_buyer_use_crawl",
        )
        buyer_use_extract = st.checkbox(
            "Use Tavily extract for top URLs",
            value=cfg.buyer_research_use_extract,
            key="s_buyer_use_extract",
        )
        save_buyer_research = st.form_submit_button("Save buyer research", type="primary")

# ---------- Content generation types ----------
with st.expander("Content generation types", expanded=True):
    with st.form("settings_content_types_form", clear_on_submit=False):
        st.caption(
            "Turning off call scripts and LinkedIn DMs reduces LLM cost. "
            "Only checked types are generated; unchecked types are skipped "
            "cleanly (no LLM call, no placeholder record). Existing saved "
            "content is never deleted."
        )
        gen_email = st.checkbox(
            "Generate emails",
            value=cfg.generate_email_enabled,
            key="s_gen_email",
        )
        gen_call = st.checkbox(
            "Generate call scripts",
            value=cfg.generate_call_script_enabled,
            key="s_gen_call_script",
        )
        gen_dm = st.checkbox(
            "Generate LinkedIn DMs",
            value=cfg.generate_linkedin_dm_enabled,
            key="s_gen_linkedin_dm",
        )
        save_content_types = st.form_submit_button("Save content types", type="primary")

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
        save_workspace_icp_config(updated, workspace_id=_selected_ws_id)
    except Exception as exc:
        st.error(f"Failed to save {label}: {exc}")
        return
    st.cache_data.clear()
    _ws_label = _selected_ws.get("name") if _selected_ws else "workspace"
    st.success(f"Saved for workspace **{_ws_label}** — {label} saved.")


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

if save_buyer_research:
    cfg.buyer_research_news_window_days = int(buyer_news_window)
    cfg.buyer_research_use_research = bool(buyer_use_research)
    cfg.buyer_research_use_crawl = bool(buyer_use_crawl)
    cfg.buyer_research_use_extract = bool(buyer_use_extract)
    _persist(cfg, "Buyer research")

if save_content_types:
    cfg.generate_email_enabled = bool(gen_email)
    cfg.generate_call_script_enabled = bool(gen_call)
    cfg.generate_linkedin_dm_enabled = bool(gen_dm)
    _persist(cfg, "Content generation types")

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
                save_workspace_icp_config(default_icp_config(), workspace_id=_selected_ws_id)
            except Exception as exc:
                st.error(f"Reset failed: {exc}")
                st.session_state.pop(pending_key, None)
                st.session_state.pop(pending_at_key, None)
            else:
                st.session_state.pop(pending_key, None)
                st.session_state.pop(pending_at_key, None)
                st.cache_data.clear()
                st.success("ICP config reset to defaults.")
                st.rerun()

# ---------- Restore from legacy config (OSP workspace only) ----------
_default_ws_id = get_default_workspace_id()
if _selected_ws_id is not None and _selected_ws_id == _default_ws_id:
    st.divider()
    with st.container(border=True):
        st.markdown("**Restore OSP settings from legacy config file**")
        st.caption(
            "If OSP settings were lost or replaced during migration, this reads the "
            "committed `data/icp_config.json` file and writes it back to the OSP workspace. "
            "Only affects the OSP workspace. Preview is shown before applying."
        )

        _restore_preview_key = "restore_legacy_preview_open"
        _restore_confirm_key = "restore_legacy_confirm_pending"
        _restore_confirm_at_key = "restore_legacy_confirm_at"
        _RESTORE_TTL = 15.0

        if not st.session_state.get(_restore_preview_key, False):
            if st.button("Preview legacy config", key="restore_legacy_preview_btn", type="secondary"):
                st.session_state[_restore_preview_key] = True
                st.rerun()
        else:
            try:
                _restore_info = restore_osp_from_legacy_config()
            except Exception as _exc:
                st.error(f"Could not read legacy config: {_exc}")
                _restore_info = None

            if _restore_info:
                if not _restore_info["file_exists"]:
                    st.warning(
                        f"Legacy config file not found at: `{_restore_info['file_path']}`  \n"
                        "Nothing to restore."
                    )
                    st.session_state.pop(_restore_preview_key, None)
                else:
                    _file_name = _restore_info.get("file_company_name") or "—"
                    _db_name = _restore_info.get("current_db_company_name") or "(not set)"
                    st.markdown(
                        f"**File company name:** `{_file_name}`  \n"
                        f"**Current DB company name:** `{_db_name}`"
                    )
                    if _restore_info.get("config"):
                        with st.expander("Preview full legacy config", expanded=False):
                            import json as _json_mod
                            st.code(_json_mod.dumps(_restore_info["config"], indent=2), language="json")

                    _restore_pending = st.session_state.get(_restore_confirm_key, False)
                    _restore_at = float(st.session_state.get(_restore_confirm_at_key, 0.0))
                    if _restore_pending and (time.monotonic() - _restore_at) > _RESTORE_TTL:
                        _restore_pending = False
                        st.session_state[_restore_confirm_key] = False
                        st.caption(":gray[Restore confirmation expired.]")

                    rc1, rc2 = st.columns(2)
                    with rc1:
                        if not _restore_pending:
                            if st.button(
                                "Restore OSP settings from file",
                                key="restore_legacy_apply_btn",
                                type="primary",
                            ):
                                st.session_state[_restore_confirm_key] = True
                                st.session_state[_restore_confirm_at_key] = time.monotonic()
                                st.rerun()
                        else:
                            if st.button(
                                "Confirm restore — this overwrites current OSP settings",
                                key="restore_legacy_confirm_btn",
                                type="primary",
                            ):
                                try:
                                    backfill_osp_icp_config(force=True)
                                except Exception as _exc:
                                    st.error(f"Restore failed: {_exc}")
                                else:
                                    st.session_state.pop(_restore_preview_key, None)
                                    st.session_state.pop(_restore_confirm_key, None)
                                    st.session_state.pop(_restore_confirm_at_key, None)
                                    st.cache_data.clear()
                                    st.success(
                                        f"OSP settings restored from file. "
                                        f"Company name: **{_file_name}**"
                                    )
                                    st.rerun()
                    with rc2:
                        if st.button("Cancel", key="restore_legacy_cancel_btn", type="secondary"):
                            st.session_state.pop(_restore_preview_key, None)
                            st.session_state.pop(_restore_confirm_key, None)
                            st.session_state.pop(_restore_confirm_at_key, None)
                            st.rerun()
