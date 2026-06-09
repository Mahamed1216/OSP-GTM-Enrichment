"""Lead Sources — connect each workspace to the OSP Lead Engine API.

Pull-based import from:  GET /api/v1/clients/{slug}/contacts
Auth:                    Authorization: Bearer <api_key>
Test connection:         GET /api/v1/health  →  GET /api/v1/clients/{slug}
Preview:                 fetch without importing (no DB writes)

Phase 7 scope — explicitly excluded:
- POST /api/v1/clients/{slug}/runs (sourcing triggers) — not in this phase
- Auto-pipeline after import
- Auto-send or Instantly push
- Overwriting existing lead fields

Security:
- API key stored in workspace row; masked in UI (last 4 chars only)
- NEVER log the bearer token
- Recommend rotating key if it was shared over Slack
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.lib.workspace_state import get_current_workspace_id, render_workspace_banner
from app.styles import inject_styles
from src.db import ensure_tables
from src.lead_source.client import LeadSourceClient
from src.lead_source.ingest import get_recent_imports, preview_contacts, run_import
from src.lead_source.settings import (
    LeadSourceConfig,
    load_lead_source_config,
    mask_api_key,
    save_lead_source_config,
)

inject_styles()
ensure_tables("lead_source_imports")

st.title("Lead Sources")
st.caption(
    "Connect this workspace to the OSP Lead Engine API to pull sourced contacts "
    "into the local pipeline. Imports are operator-initiated — nothing runs automatically."
)

render_workspace_banner()
st.divider()

ws_id = get_current_workspace_id()
if ws_id is None:
    st.error("No workspace selected. Use the sidebar to choose a workspace.")
    st.stop()

cfg = load_lead_source_config(ws_id)

# ---------------------------------------------------------------------------
# Section 1 — Connection settings
# ---------------------------------------------------------------------------
st.subheader("Lead source integration")

with st.form("lead_source_settings_form"):
    enabled = st.checkbox(
        "Lead source enabled",
        value=cfg.enabled,
    )
    api_base_url = st.text_input(
        "API base URL",
        value=cfg.api_base_url,
        placeholder="https://leads.osp.tools",
    )
    client_slug = st.text_input(
        "Client slug",
        value=cfg.client_slug,
        placeholder="osp",
        help="Used in /api/v1/clients/{slug}/contacts",
    )
    col_icp, col_status = st.columns(2)
    with col_icp:
        default_icp = st.text_input(
            "Default ICP filter",
            value=cfg.default_icp,
            placeholder="saas-cto",
            help="Optional ICP slug passed as ?icp= on each fetch",
        )
    with col_status:
        default_status = st.selectbox(
            "Default status filter",
            options=["", "enriched", "verified", "pending", "failed"],
            index=["", "enriched", "verified", "pending", "failed"].index(
                cfg.default_status_filter if cfg.default_status_filter in ["", "enriched", "verified", "pending", "failed"] else ""
            ),
            help="Contacts with this enrichment_status only (leave blank for all)",
        )
    include_suppressed = st.checkbox(
        "Include suppressed contacts",
        value=cfg.include_suppressed,
        help="When enabled, suppressed contacts are included in fetches",
    )
    daily_fetch_limit = st.number_input(
        "Default fetch limit",
        min_value=1, max_value=500, value=cfg.daily_fetch_limit, step=5,
    )

    if cfg.api_key:
        st.caption(f"API key on file: `{mask_api_key(cfg.api_key)}`")
    new_api_key = st.text_input(
        "API key (leave blank to keep existing)",
        value="",
        type="password",
        help="Authorization: Bearer <api_key>. Stored in the workspace row.",
    )
    save_clicked = st.form_submit_button("Save settings")

if save_clicked:
    resolved_key = new_api_key.strip() if new_api_key.strip() else cfg.api_key
    updated = LeadSourceConfig(
        enabled=enabled,
        api_base_url=api_base_url.strip(),
        api_key=resolved_key,
        client_slug=client_slug.strip(),
        default_icp=default_icp.strip(),
        default_status_filter=default_status,
        include_suppressed=include_suppressed,
        daily_fetch_limit=int(daily_fetch_limit),
        last_fetched_at=cfg.last_fetched_at,
        last_fetch_status=cfg.last_fetch_status,
        last_fetch_result_count=cfg.last_fetch_result_count,
    )
    try:
        save_lead_source_config(updated, ws_id)
        cfg = updated
        st.success("Settings saved.")
        if new_api_key.strip():
            st.warning(
                "Security reminder: if this API key was shared via Slack or any "
                "unsecured channel, rotate it at leads.osp.tools after verifying "
                "the connection works."
            )
    except Exception as exc:
        st.error(f"Save failed: {exc}")

if cfg.last_fetched_at:
    st.caption(
        f"Last fetch: {cfg.last_fetched_at[:19]}  ·  "
        f"Status: {cfg.last_fetch_status or '—'}  ·  "
        f"Created: {cfg.last_fetch_result_count or 0}"
    )

st.divider()

# ---------------------------------------------------------------------------
# Section 2 — Test connection
# ---------------------------------------------------------------------------
st.subheader("Test connection")

_ready = bool(cfg.api_base_url and cfg.client_slug)

if st.button(
    "Test lead source connection",
    disabled=not _ready,
    help="Calls /api/v1/health (no auth) then /api/v1/clients/{slug} (with Bearer auth)",
):
    with st.spinner("Testing…"):
        try:
            c = LeadSourceClient(cfg.api_base_url, cfg.api_key)
            r = c.test_connection(cfg.client_slug)
        except Exception as exc:
            r = {"ok": False, "error": str(exc)}

    if r.get("ok"):
        st.success(
            f"Connected — health {r.get('health_status_code')}, "
            f"client '{r.get('client_name')}' ({r.get('client_status')})"
        )
        if r.get("icp_slugs"):
            st.caption("Available ICP slugs: " + ", ".join(r["icp_slugs"]))
    else:
        st.error(f"Connection failed: {r.get('error', 'Unknown error')}")
        if r.get("health_status_code"):
            st.caption(f"Health HTTP: {r['health_status_code']}")
        if r.get("client_status_code"):
            st.caption(f"Client config HTTP: {r['client_status_code']}")

st.divider()

# ---------------------------------------------------------------------------
# Section 3 — Filters shared by preview + import
# ---------------------------------------------------------------------------
st.subheader("Contact filters")

col1, col2, col3 = st.columns([2, 2, 1])
with col1:
    run_icp = st.text_input(
        "ICP filter (this run)",
        value=cfg.default_icp,
        placeholder="leave blank for all",
        key="run_icp",
    )
    run_created_after = st.text_input(
        "Created after (ISO date, optional)",
        value="",
        placeholder="2026-01-01",
        key="run_created_after",
    )
with col2:
    run_status = st.selectbox(
        "Enrichment status filter",
        options=["", "enriched", "verified", "pending", "failed"],
        index=["", "enriched", "verified", "pending", "failed"].index(
            cfg.default_status_filter if cfg.default_status_filter in ["", "enriched", "verified", "pending", "failed"] else ""
        ),
        key="run_status",
    )
    run_created_before = st.text_input(
        "Created before (ISO date, optional)",
        value="",
        placeholder="2026-12-31",
        key="run_created_before",
    )
with col3:
    run_suppressed = st.checkbox(
        "Include suppressed",
        value=cfg.include_suppressed,
        key="run_suppressed",
    )
    run_limit = st.number_input(
        "Limit",
        min_value=1, max_value=500,
        value=cfg.daily_fetch_limit,
        step=5,
        key="run_limit",
    )

_filter_kwargs = dict(
    icp=run_icp.strip() or None,
    status_filter=run_status or None,
    include_suppressed=run_suppressed,
    created_after=run_created_after.strip() or None,
    created_before=run_created_before.strip() or None,
)

st.divider()

# ---------------------------------------------------------------------------
# Section 4 — Preview contacts
# ---------------------------------------------------------------------------
st.subheader("Preview contacts")
st.caption("Fetches and displays contacts without importing them.")

if st.button(
    "Preview contacts",
    disabled=not (cfg.enabled and cfg.api_base_url and cfg.client_slug and cfg.api_key),
    key="btn_preview",
):
    with st.spinner(f"Fetching preview ({int(run_limit)} contacts)…"):
        try:
            rows = preview_contacts(
                ws_id,
                cfg.client_slug,
                cfg.api_base_url,
                cfg.api_key,
                limit=int(run_limit),
                **_filter_kwargs,
            )
        except Exception as exc:
            st.error(f"Preview failed: {type(exc).__name__}: {exc}")
            rows = []

    if rows:
        preview_data = [
            {
                "External ID": c.get("id", ""),
                "Name": f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
                "Title": c.get("title", ""),
                "Company": c.get("company_name", ""),
                "Email": c.get("email", ""),
                "Domain": c.get("company_domain", ""),
                "ICP": c.get("icp", ""),
                "Tier": c.get("tier", ""),
                "Tier score": c.get("tier_score", ""),
                "Enrich status": c.get("enrichment_status", ""),
                "Created at": str(c.get("created_at", ""))[:10],
            }
            for c in rows
        ]
        st.dataframe(pd.DataFrame(preview_data), use_container_width=True)
        st.caption(f"{len(rows)} contact(s) shown — not yet imported.")
    elif not rows:
        st.info("No contacts matched the filters.")

st.divider()

# ---------------------------------------------------------------------------
# Section 5 — Import contacts
# ---------------------------------------------------------------------------
st.subheader("Import contacts")

if not cfg.enabled:
    st.info("Enable the lead source in settings above before importing.")
elif not (cfg.api_base_url and cfg.client_slug and cfg.api_key):
    st.warning("Complete the settings (URL, slug, API key) before importing.")
else:
    if st.button("Import contacts", type="primary", key="btn_import"):
        with st.spinner(f"Importing up to {int(run_limit)} contacts from '{cfg.client_slug}'…"):
            try:
                import_result = run_import(
                    ws_id,
                    cfg.client_slug,
                    cfg.api_base_url,
                    cfg.api_key,
                    limit=int(run_limit),
                    **_filter_kwargs,
                )
                st.session_state["lead_source_imported_ids"] = import_result.created_lead_ids
                st.success(
                    f"Import complete — "
                    f"fetched {import_result.fetched}, "
                    f"created {import_result.created}, "
                    f"updated {import_result.updated}, "
                    f"skipped {import_result.skipped}, "
                    f"errors {import_result.errors}."
                )
                if import_result.skip_reasons:
                    with st.expander("Skip reasons"):
                        for reason, count in import_result.skip_reasons.items():
                            st.write(f"- **{reason}**: {count}")
                cfg = load_lead_source_config(ws_id)
            except Exception as exc:
                st.error(f"Import failed: {type(exc).__name__}: {exc}")

st.divider()

# ---------------------------------------------------------------------------
# Section 6 — Recent imports
# ---------------------------------------------------------------------------
st.subheader("Recent imports")

recent = get_recent_imports(ws_id, limit=5)
if not recent:
    st.caption("No imports recorded for this workspace yet.")
else:
    for imp in recent:
        started = str(imp["started_at"])[:19] if imp["started_at"] else "—"
        badge = {"completed": "✅", "failed": "❌", "running": "⏳"}.get(imp["status"], "❓")
        slug_info = imp["client_slug"] or "?"
        if imp.get("icp_filter"):
            slug_info += f" / icp:{imp['icp_filter']}"
        with st.expander(
            f"{badge} {started}  ·  {slug_info}  ·  "
            f"created {imp['created_count']} / skipped {imp['skipped_count']} / "
            f"errors {imp['error_count']}"
        ):
            cols = st.columns(4)
            cols[0].metric("Fetched", imp["fetched_count"])
            cols[1].metric("Created", imp["created_count"])
            cols[2].metric("Updated", imp["updated_count"])
            cols[3].metric("Skipped", imp["skipped_count"])
            if imp.get("error_message"):
                st.error(imp["error_message"])
            if imp.get("raw_summary", {}) and imp["raw_summary"].get("skip_reasons"):
                st.caption("Skip reasons: " + str(imp["raw_summary"]["skip_reasons"]))

st.divider()

# ---------------------------------------------------------------------------
# Section 7 — Pipeline handoff
# ---------------------------------------------------------------------------
st.subheader("Run pipeline for imported leads")

imported_ids: list[int] = st.session_state.get("lead_source_imported_ids", [])
if not imported_ids:
    st.caption("Import contacts above — lead IDs will appear here for pipeline handoff.")
else:
    st.success(f"{len(imported_ids)} lead(s) ready from the last import.")
    st.text_area(
        "Imported lead IDs (copy to Run Pipeline if needed)",
        value=", ".join(str(i) for i in imported_ids),
        height=80,
    )
    if st.button("Clear"):
        st.session_state.pop("lead_source_imported_ids", None)
        st.rerun()
    st.info(
        "Go to **Run Pipeline**, select these leads, enrich → score → generate content. "
        "Do not use auto-send — review all content before sending."
    )
