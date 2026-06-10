"""Lead Sources — connect each workspace to the OSP Lead Engine API.

Phase 7: Pull-based import + preview.
Phase 8: Evergreen automation — scheduled import + auto score + content.

Auto-process skips enrichment entirely (OSP Lead Engine pre-enriches contacts).
No emails auto-sent. No Instantly push. No POST /runs called.

Scheduling is external:
  Option A CLI: python -m src.lead_source.scheduler --workspace-id N
  Option B HTTP: POST /api/lead-source/run-scheduled  X-Job-Secret: ...
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
from src.lead_source.scheduler import run_import_and_process_sync
from src.lead_source.settings import (
    LeadSourceConfig,
    load_lead_source_config,
    mask_api_key,
    reset_import_cursor,
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
    # Use cfg.model_dump() as the base so all metadata and cursor fields
    # (next_offset, last_auto_run_*, etc.) are preserved automatically.
    updated = LeadSourceConfig(
        **{
            **cfg.model_dump(),
            "enabled": enabled,
            "api_base_url": api_base_url.strip(),
            "api_key": resolved_key,
            "client_slug": client_slug.strip(),
            "default_icp": default_icp.strip(),
            "default_status_filter": default_status,
            "include_suppressed": include_suppressed,
            "daily_fetch_limit": int(daily_fetch_limit),
        }
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
# Section 7 — Evergreen automation (Phase 8)
# ---------------------------------------------------------------------------
st.subheader("Evergreen automation")
st.caption(
    "When enabled, a scheduled job (Render Cron / GitHub Actions) runs "
    "`python -m src.lead_source.scheduler` to import + process new contacts. "
    "Scoring and content generation run; enrichment is skipped (OSP Lead Engine "
    "pre-enriches contacts). No emails are sent automatically."
)

with st.form("automation_settings_form"):
    auto_import = st.checkbox(
        "Enable scheduled import",
        value=cfg.auto_import_enabled,
        help="When checked, the scheduler will fetch new contacts for this workspace.",
    )
    auto_process = st.checkbox(
        "Enable auto score + content generation for newly imported leads",
        value=cfg.auto_process_enabled,
        help="After import, run scoring and content generation. Enrichment is skipped.",
    )
    freq = st.selectbox(
        "Schedule frequency (hint — actual scheduling is external)",
        options=["manual", "daily", "hourly"],
        index=["manual", "daily", "hourly"].index(
            cfg.schedule_frequency if cfg.schedule_frequency in ("manual", "daily", "hourly") else "daily"
        ),
    )
    auto_save = st.form_submit_button("Save automation settings")

if auto_save:
    updated_auto = LeadSourceConfig(
        **{
            **cfg.model_dump(),
            "auto_import_enabled": auto_import,
            "auto_process_enabled": auto_process,
            "schedule_frequency": freq,
        }
    )
    try:
        save_lead_source_config(updated_auto, ws_id)
        cfg = updated_auto
        st.success("Automation settings saved.")
    except Exception as exc:
        st.error(f"Save failed: {exc}")

# Last auto run summary
if cfg.last_auto_run_at:
    ac1, ac2, ac3, ac4 = st.columns(4)
    ac1.metric("Last auto run", str(cfg.last_auto_run_at)[:16])
    ac2.metric("Status", cfg.last_auto_run_status or "—")
    ac3.metric("Created", cfg.last_auto_run_created or 0)
    ac4.metric("Scored / Content", f"{cfg.last_auto_run_scored or 0} / {cfg.last_auto_run_content or 0}")

# ---------- Import cursor ----------
st.markdown("**Import cursor** — tracks progress through the remote contact list")
st.caption(
    "Each scheduled run fetches from *Next offset* and advances the cursor by the "
    "number of contacts returned. When the end of the list is reached the cursor "
    "resets to 0 automatically. Manual imports always start at offset 0."
)

ic1, ic2, ic3, ic4 = st.columns(4)
ic1.metric("Next scheduled offset", cfg.next_offset)
ic2.metric("Last auto fetched", cfg.last_auto_run_fetched if cfg.last_auto_run_fetched is not None else "—")
ic3.metric("Last auto created", cfg.last_auto_run_created if cfg.last_auto_run_created is not None else "—")
ic4.metric("Last auto skipped", cfg.last_auto_run_skipped if cfg.last_auto_run_skipped is not None else "—")

if st.button("Reset import cursor", key="btn_reset_cursor", type="secondary",
             help="Set next_offset back to 0. The next scheduled run will start from the beginning of the contact list."):
    try:
        reset_import_cursor(ws_id)
        cfg = load_lead_source_config(ws_id)
        st.success("Import cursor reset to 0 — next scheduled run starts from the beginning.")
        st.rerun()
    except Exception as _exc:
        st.error(f"Reset failed: {_exc}")

st.divider()

# Buttons
_can_run = bool(cfg.enabled and cfg.api_base_url and cfg.client_slug and cfg.api_key)

bcol1, bcol2, bcol3 = st.columns(3)

with bcol1:
    if st.button(
        "Run import now",
        disabled=not _can_run,
        key="btn_manual_import",
        help="Fetch and import new contacts only. No scoring or content generation.",
    ):
        with st.spinner("Importing…"):
            try:
                r = run_import(
                    ws_id,
                    cfg.client_slug,
                    cfg.api_base_url,
                    cfg.api_key,
                    limit=cfg.daily_fetch_limit,
                    icp=cfg.default_icp or None,
                    status_filter=cfg.default_status_filter or None,
                    include_suppressed=cfg.include_suppressed,
                )
                st.session_state["lead_source_imported_ids"] = r.created_lead_ids
                st.success(
                    f"Import done — fetched {r.fetched}, created {r.created}, "
                    f"skipped {r.skipped}."
                )
                cfg = load_lead_source_config(ws_id)
            except Exception as exc:
                st.error(f"Import failed: {exc}")

with bcol2:
    if st.button(
        "Run import + process now",
        disabled=not _can_run,
        key="btn_manual_import_process",
        type="primary",
        help="Import new contacts, then score and generate content. Enrichment skipped.",
    ):
        with st.spinner("Importing and processing (may take a few minutes)…"):
            try:
                result = run_import_and_process_sync(ws_id)
                if result.get("skipped"):
                    st.warning(f"Skipped: {result.get('reason')}")
                else:
                    st.session_state["lead_source_imported_ids"] = []
                    st.success(
                        f"Done — created {result.get('created',0)}, "
                        f"scored {result.get('scored_count',0)}, "
                        f"content {result.get('content_generated_count',0)}, "
                        f"enrichment skipped {result.get('enrichment_skipped_count',0)}."
                    )
                cfg = load_lead_source_config(ws_id)
            except Exception as exc:
                st.error(f"Run failed: {exc}")

with bcol3:
    if st.button(
        "Preview next batch",
        disabled=not _can_run,
        key="btn_preview_next",
        help="Show what the next scheduled import would fetch (no DB writes).",
    ):
        with st.spinner("Fetching preview…"):
            try:
                rows = preview_contacts(
                    ws_id, cfg.client_slug, cfg.api_base_url, cfg.api_key,
                    limit=cfg.daily_fetch_limit,
                    icp=cfg.default_icp or None,
                    status_filter=cfg.default_status_filter or None,
                    include_suppressed=cfg.include_suppressed,
                )
                if rows:
                    st.dataframe(
                        pd.DataFrame([{
                            "ID": c.get("id",""), "Name": f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
                            "Email": c.get("email",""), "Company": c.get("company_name",""),
                            "ICP": c.get("icp",""), "Tier": c.get("tier",""),
                            "Status": c.get("enrichment_status",""),
                        } for c in rows]),
                        use_container_width=True,
                    )
                    st.caption(f"{len(rows)} contacts in next batch — not imported.")
                else:
                    st.info("No contacts matched the current filters.")
            except Exception as exc:
                st.error(f"Preview failed: {exc}")

st.caption(
    "True scheduling requires an external trigger. "
    "CLI: `python -m src.lead_source.scheduler --workspace-id N`  |  "
    "HTTP: `POST /api/lead-source/run-scheduled` (webhook server)."
)

st.divider()

# ---------------------------------------------------------------------------
# Section 8 — Pipeline handoff
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
