"""Engagement — sync from Instantly, KPIs, reply-rate trend, winners library.

Phase 7 build, three sub-phases:
- 7a: skeleton, sync controls, KPIs (this file).
- 7b: reply-rate chart, winners/negatives library tables.
- 7c: demote button, recent engagement events, is_active migration.

All backend calls go through the already-exposed helpers in
``src/feedback/engagement.py``, ``src/feedback/learning.py``, and
``app/lib/db_queries.py``. This page is presentational.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

import pandas as pd

from app.lib.async_runner import run_async
from app.lib.components import kpi_card
from app.lib.db_queries import (
    kpi_counts,
    last_sync_time,
    latest_instantly_snapshot,
    local_sent_count,
    recent_engagement,
    reply_rate_series,
    sent_emails,
)
from app.lib.workspace_state import get_current_workspace_id, render_workspace_banner
from app.styles import inject_styles

inject_styles()
from src.content.winners import (
    list_all_negatives,
    list_all_winners,
    set_negative_active,
    set_winner_active,
)
from src.feedback.engagement import (
    CampaignAnalyticsMismatch,
    search_positive_fields,
    sync_campaign_analytics,
    sync_engagement,
)
from src.feedback.learning import cleanup_invalid_winners, process_ratings, promote_winners
from src.feedback.reply_agent import (
    classify_and_draft_reply,
    save_reply_draft,
)
from src.feedback.reply_sync import (
    ACTIVE_STATUSES,
    POSITIVE_CLASSIFICATIONS,
    STATUS_APPROVED,
    STATUS_ARCHIVED,
    STATUS_MANUAL_SEND,
    STATUS_MISSING_TEXT,
    STATUS_NEEDS_HUMAN,
    STATUS_NEEDS_REVIEW,
    STATUS_SENT,
    STATUS_SKIPPED,
    archive_non_opportunity_threads,
    archive_placeholder_threads,
    archive_stale_threads,
    create_draft_from_replied_lead,
    ensure_reply_agent_schema,
    get_local_replied_leads,
    get_reply_queue,
    get_send_blocked_reason,
    sync_reply_queue,
    try_send_reply,
    update_reply_thread,
)
from src.feedback.self_improvement import (
    DEFAULT_BOUNCE_RATE_MAX,
    DEFAULT_OPEN_RATE_TARGET,
    DEFAULT_REPLY_RATE_TARGET,
    LOOP_DIAGNOSE,
    LOOP_DRAFT,
    LOOP_READY,
    LOOP_WAIT,
    MIN_SAMPLE_FOR_RECOMMENDATION,
    REPLY_DIAGNOSE_HOURS,
    REPLY_POSITIVE_SIGNAL_GATE_HOURS,
    SAMPLE_LOW_CONF_MAX,
    approve_recommendation,
    archive_recommendation,
    diagnose,
    kpi_view,
    latest_send_info,
    list_recommendations,
    performance_by_prompt_version,
    reject_recommendation,
    rollback_recommendation,
    save_as_draft,
    save_recommendation,
)

DEMOTE_TTL = 10.0

_KIND_LABELS = {"email": "Email", "call_script": "Call Script", "linkedin_msg": "LinkedIn DM"}
_TYPE_FILTER_OPTIONS = ["All", "email", "call_script", "linkedin_msg"]


@st.cache_data(ttl=15)
def _kpi_counts_cached(workspace_id: int | None) -> dict[str, int]:
    return kpi_counts(workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _instantly_snapshot_cached(workspace_id: int | None) -> dict | None:
    return latest_instantly_snapshot(workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _local_sent_cached(workspace_id: int | None) -> int:
    return local_sent_count(workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _perf_by_prompt_cached() -> list[dict]:
    return performance_by_prompt_version()


@st.cache_data(ttl=15)
def _recommendations_cached(workspace_id: int | None = None) -> list[dict]:
    return list_recommendations(limit=10, workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _last_sync_cached(workspace_id: int | None) -> datetime | None:
    return last_sync_time(workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _reply_rate_cached(days: int, workspace_id: int | None = None) -> pd.DataFrame:
    return reply_rate_series(days=days, workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _winners_cached(workspace_id: int | None = None) -> list[dict]:
    return list_all_winners(workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _negatives_cached() -> list[dict]:
    return list_all_negatives()


@st.cache_data(ttl=15)
def _recent_engagement_cached(limit: int, workspace_id: int | None = None) -> "pd.DataFrame":
    return recent_engagement(limit=limit, workspace_id=workspace_id)


@st.cache_data(ttl=15)
def _sent_emails_cached(workspace_id: int | None = None) -> "pd.DataFrame":
    return sent_emails(workspace_id=workspace_id)


def _format_event_when(ts) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    # Route every event timestamp through the shared ET formatter so the
    # engagement events list, sent folder, and recommendation history
    # all render consistently in Eastern.
    from app.lib.formatters import format_et
    return format_et(ts)


def _event_status_icons(row) -> str:
    parts = []
    if row.get("bounced"):
        parts.append("❌ bounced")
    if row.get("replied"):
        parts.append("✉️ reply")
    if row.get("clicked"):
        parts.append("🔗 click")
    if row.get("opened"):
        parts.append("👁 open")
    if row.get("delivered"):
        parts.append("✓ deliv")
    elif row.get("sent"):
        parts.append("✓ sent")
    return " · ".join(parts) if parts else "—"


def _entry_body(entry: dict) -> str:
    """Pull the body from either the v2 `content.body` field or the legacy
    top-level `body`. Mirrors src/content/winners._entry_subject_body."""
    content = entry.get("content") or {}
    if isinstance(content, dict) and content.get("body"):
        return str(content["body"])
    return str(entry.get("body") or "")


def _entry_subject(entry: dict) -> str | None:
    content = entry.get("content") or {}
    if isinstance(content, dict) and content.get("subject"):
        return str(content["subject"])
    return entry.get("subject")


_REASON_FILTER_OPTIONS = [
    "All",
    "Seed",
    "Manual approval",
    "Positive reply",
    "Opportunity",
    "Archived",
]

# Maps UI filter label → winner_reason values that match.
_REASON_FILTER_MAP: dict[str, set[str]] = {
    "Seed": {"seed"},
    "Manual approval": {"manual_positive_rating"},
    "Positive reply": {"positive_reply"},
    "Opportunity": {"opportunity", "booked_meeting", "conversion"},
    "Archived": set(),   # handled separately — show is_active=False
}


def _library_dataframe(
    entries: list[dict],
    type_filter: str,
    reason_filter: str = "All",
) -> pd.DataFrame:
    rows = []
    for e in entries:
        ct = e.get("content_type") or "email"
        if type_filter != "All" and ct != type_filter:
            continue

        # Apply reason filter.
        is_active = bool(e.get("is_active", True))
        reason = e.get("winner_reason") or "—"
        if reason_filter == "Archived":
            if is_active:
                continue
        elif reason_filter != "All":
            allowed = _REASON_FILTER_MAP.get(reason_filter, set())
            if reason not in allowed:
                continue

        body = _entry_body(e)
        snippet = body[:80].replace("\n", " ").strip()
        if len(body) > 80:
            snippet += "…"
        rows.append({
            "id": e.get("id"),
            "Active": "✓" if is_active else "—",
            "Reason": reason,
            "Type": _KIND_LABELS.get(ct, ct),
            "Source": e.get("source") or "—",
            "Added": (e.get("added_at") or "")[:10],
            "Snippet": snippet,
        })
    return pd.DataFrame.from_records(
        rows,
        columns=["id", "Active", "Reason", "Type", "Source", "Added", "Snippet"],
    )


def _format_pct(num: int, denom: int) -> str:
    if not denom:
        return "—"
    return f"{(num / denom) * 100:.1f}%"


def _format_timestamp(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    # Eastern Time via the shared formatter so "Last synced from
    # Instantly", recommendation history, and approval timestamps
    # all read the same.
    from app.lib.formatters import format_et
    return format_et(ts)


# Ensure workspace schema (tables + workspace_id columns) is in place before
# any DB query on this page.  init_db() is normally called by main.py, but
# on Streamlit Cloud the session state used by the old guard can survive a
# server restart, causing init_db() to be skipped for reconnecting sessions.
# This call is a fast no-op if init_db() already ran for this process.
from src.workspace import ensure_workspace_schema as _ensure_ws

if not _ensure_ws():
    st.warning(
        "⚠️ Workspace migration is initializing. "
        "Refresh the page in a few seconds to continue."
    )
    st.stop()

_ws_id = get_current_workspace_id()

st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Engagement.</h1>'
    '<p class="hero-sublabel">Replies, opens, bounces, sends. The loop closing.</p>'
    '</div>',
    unsafe_allow_html=True,
)

render_workspace_banner()

# ---------- Sync controls ----------
_snapshot = _instantly_snapshot_cached(workspace_id=_ws_id)

# Phase 4: show workspace campaign context for safety
_current_ws = None
try:
    from app.lib.workspace_state import get_current_workspace as _get_cw
    from src.workspace import get_campaign_id_for_workspace as _get_cid, get_api_key_source as _get_aksrc
    _current_ws = _get_cw()
    if _current_ws:
        _ws_campaign_id = _get_cid(_ws_id) or "—"
        _ws_api_src = _get_aksrc(_ws_id)
        st.info(
            f"Current workspace: **{_current_ws.get('name')}**  "
            f"·  Campaign ID: `{_ws_campaign_id}`  "
            f"·  API key source: {_ws_api_src}"
        )
except Exception:
    pass

if _snapshot is not None:
    st.caption(
        f"Last synced from Instantly: {_format_timestamp(_snapshot.get('synced_at'))} "
        f"(weekdays 9 AM ET via GitHub Actions)."
    )
else:
    st.caption("No engagement data synced yet for this workspace — hit the button below or wait for the 9 AM ET job.")

sync_col, promote_col = st.columns(2)
with sync_col:
    sync_clicked = st.button(
        "Sync engagement from Instantly",
        type="primary",
        key="eng_sync_btn",
        help=(
            "Pulls campaign analytics + per-lead engagement from Instantly. "
            "Instantly is the source of truth for the metrics row below."
        ),
    )
with promote_col:
    promote_clicked = st.button(
        "Promote winners now",
        type="secondary",
        key="eng_promote_btn",
        help=(
            "Promote engagement replies to the winners library and roll up "
            "thumbs-up/down ratings into winners/negatives."
        ),
    )

if sync_clicked:
    analytics_result: dict | None = None
    per_lead: dict | None = None
    sync_error: str | None = None
    mismatch_debug: dict | None = None
    local_before = _local_sent_cached(workspace_id=_ws_id)
    try:
        with st.spinner("Syncing from Instantly…"):
            analytics_result = run_async(sync_campaign_analytics(workspace_id=_ws_id))
            per_lead = run_async(sync_engagement(workspace_id=_ws_id))
    except CampaignAnalyticsMismatch as exc:
        sync_error = str(exc)
        mismatch_debug = exc.debug
    except Exception as exc:
        sync_error = f"{type(exc).__name__}: {exc}"

    if sync_error is not None:
        st.error(f"Sync failed: {sync_error}")
        if mismatch_debug is not None:
            # Surface the debug bundle so the operator can tell whether
            # INSTANTLY_CAMPAIGN_ID is wrong or Instantly's response shape
            # changed.
            st.json(mismatch_debug)
    elif analytics_result is not None:
        st.cache_data.clear()
        contacted = int(analytics_result.get("contacted_count") or 0)
        sent_remote = int(analytics_result.get("emails_sent_count") or 0)
        opens = int(analytics_result.get("open_count") or 0)
        replies = int(analytics_result.get("reply_count") or 0)
        bounces = int(analytics_result.get("bounced_count") or 0)
        denom = sent_remote or 1
        st.success(
            f"Synced — selected campaign "
            f"`{analytics_result.get('selected_campaign_id')}` "
            f"({analytics_result.get('selected_campaign_name') or 'unnamed'}) · "
            f"local sent before sync: {local_before} · "
            f"Instantly sequence started: {contacted} · "
            f"sent: {sent_remote} · opens: {opens} · replies: {replies} · "
            f"bounces: {bounces} · "
            f"open rate: {opens / denom * 100:.1f}% · "
            f"reply rate: {replies / denom * 100:.2f}% · "
            f"bounce rate: {bounces / denom * 100:.2f}% · "
            f"per-lead: {(per_lead or {}).get('synced', 0)} updated, "
            f"{(per_lead or {}).get('failed', 0)} failed."
        )
        if contacted and abs(contacted - local_before) >= 5:
            st.warning(
                f"Instantly campaign "
                f"`{analytics_result.get('selected_campaign_id')}` has "
                f"{contacted} sequence started, but local DB has "
                f"{local_before} sent records for this campaign. "
                "Investigate — manual imports or lost delivery_id rows "
                "can cause this gap."
            )
        st.rerun()

if promote_clicked:
    try:
        with st.spinner("Promoting winners and processing ratings…"):
            promo = promote_winners()
            ratings = process_ratings()
    except Exception as exc:
        st.error(f"Promotion failed: {exc}")
    else:
        st.cache_data.clear()
        st.success(
            f"Promoted {promo.get('promoted', 0)} winners "
            f"(library now {promo.get('library_size', 0)}); "
            f"ratings processed: +{ratings.get('new_winners', 0)} winners, "
            f"+{ratings.get('new_negatives', 0)} negatives."
        )
        st.rerun()

st.divider()

# ---------- KPI row (Instantly is source of truth) ----------
st.caption("Source: Instantly campaign analytics")

# `kpi_view()` is the single source for every number on this page. The
# self-improving loop below reads the SAME dict — so KPI cards and the
# loop's "current bounce rate" can never disagree on a rounding boundary.
_metrics = kpi_view(_snapshot)
sent_remote = _metrics["sent"]
contacted = _metrics["contacted"]
opens = _metrics["opens"]
replies = _metrics["replies"]
bounces = _metrics["bounces"]
positive_signals = _metrics.get("positive_signals", 0)
opportunities = _metrics.get("opportunities", 0)

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    kpi_card("Sent", f"{sent_remote:,}")
with c2:
    _open_rate_pct = _metrics.get("open_rate", 0.0) * 100
    kpi_card("Open rate", f"{_open_rate_pct:.1f}%")
with c3:
    kpi_card(
        "Reply rate",
        _format_pct(replies, sent_remote),
        numeric_font="serif",
    )
with c4:
    # Show positive replies / opportunities from Instantly — this is the
    # "interested / booked / opportunity" count, separate from total replies.
    _pos_label = (
        "Opportunities" if opportunities > 0 and opportunities >= _metrics.get("positive_replies", 0)
        else "Positive replies"
    )
    kpi_card(_pos_label, str(positive_signals) if positive_signals else "—")
with c5:
    kpi_card("Bounce rate", _format_pct(bounces, sent_remote))

_open_rate_source = _metrics.get("open_rate_source", "")
_unique_opens = _metrics.get("unique_opens")
if _snapshot is not None:
    _open_note_parts = [
        f"Sequence started: {contacted:,}",
        f"Emails sent (total): {sent_remote:,}",
        f"Total opens: {_metrics.get('opens', 0):,}",
    ]
    if _unique_opens is not None:
        _open_note_parts.append(f"Unique opens: {_unique_opens:,}")
    if _open_rate_source:
        _formula_label = (
            "unique opens / sequence started (matches Instantly UI)"
            if _open_rate_source == "unique_opens/contacted"
            else "total opens / emails sent"
        )
        _open_note_parts.append(f"Open rate formula: {_formula_label}")
    st.caption("  ·  ".join(_open_note_parts))

# Sync-hygiene warning: surfaced ONLY here, never as a self-improvement
# recommendation. The loop's delivery branch is informational; this
# warning is the operator-facing version.
_local_sent = _local_sent_cached(workspace_id=_ws_id)
if _snapshot is not None and contacted and abs(contacted - _local_sent) >= 5:
    st.warning(
        f"Sync hygiene: Instantly has {contacted:,} sequence started, but "
        f"local DB has {_local_sent:,} sent records for this campaign. "
        "Investigate the gap — this is a tracking issue, not a copy issue."
    )

st.divider()

# ---------- Reply Agent ----------
st.subheader("Reply Agent")
st.caption("Review opportunity replies, approve drafts, and send responses through Instantly.")
if _current_ws:
    st.caption(f"Current workspace: **{_current_ws.get('name')}**")

_RA_ACTION_LABELS = {
    "book_meeting": "📅 Book meeting",
    "ask_clarifying_question": "❓ Ask a clarifying question",
    "send_info": "📧 Send info",
    "route_to_human": "🚨 Route to human",
    "mark_not_interested": "🔴 Mark not interested",
    "stop_sequence": "🛑 Stop sequence (opt-out)",
    "do_not_reply": "⏭ Do not reply",
}
_RA_STATUS_ICONS = {
    STATUS_NEEDS_REVIEW: "🔔 Needs review",
    STATUS_NEEDS_HUMAN: "🚨 Needs human",
    STATUS_SENT: "✅ Sent",
    STATUS_SKIPPED: "⏭ Skipped",
    "failed": "❌ Failed",
    STATUS_MANUAL_SEND: "📋 Manual send required",
    STATUS_APPROVED: "✓ Approved",
}

# Belt-and-suspenders: ensure reply_threads table exists before any query.
# init_db() normally handles this on startup, but on Streamlit Cloud the
# Python process may not restart on a hot-deploy, leaving _db_initialized=True
# while the new table was never created.  ensure_reply_agent_schema() is NOT
# gated by _db_initialized so it always checks and creates if needed.
_reply_schema_ready = ensure_reply_agent_schema()
if not _reply_schema_ready:
    st.warning(
        "Reply Agent tables are initializing. "
        "Reboot the app or refresh the page in a few seconds to continue."
    )

# Load workspace calendar link (used for auto-drafts)
try:
    from src.workspace import get_calendar_link_for_workspace as _get_cal_link
    _ws_calendar_link = _get_cal_link(_ws_id) or ""
except Exception:
    _ws_calendar_link = ""

_tab_queue, _tab_manual, _tab_create = st.tabs(["Reply Queue", "Manual Draft Tester", "Create Draft from Replied Lead"])

# ── Tab 1: Reply Queue ────────────────────────────────────────────────────
with _tab_queue:

    # Sync button
    _rq_sync_col, _rq_info_col = st.columns([2, 4])
    with _rq_sync_col:
        _rq_sync_clicked = st.button(
            "Sync opportunity replies from Instantly",
            type="primary",
            key="rq_sync_btn",
        )
    with _rq_info_col:
        if _ws_calendar_link:
            st.caption(f"Workspace calendar link: `{_ws_calendar_link}`")
        else:
            st.caption(
                ":orange[No calendar link set for this workspace — reply drafts will ask to set up a time. "
                "Add `calendar_link` to workspace icp_config to auto-include it.]"
            )

    if _rq_sync_clicked:
        with st.spinner("Fetching opportunity replies from Instantly…"):
            try:
                _rq_sync_result = run_async(sync_reply_queue(workspace_id=_ws_id))
                st.session_state["rq_last_sync"] = _rq_sync_result
                st.cache_data.clear()
            except Exception as _rq_exc:
                st.error(f"Reply Queue sync failed: {_rq_exc}")
                _rq_sync_result = None

        if _rq_sync_result is not None:
            _rq_total    = _rq_sync_result.get("total_leads_scanned", 0)
            _rq_opp      = _rq_sync_result.get("opportunity_leads_found", 0)
            _rq_local    = _rq_sync_result.get("local_replied_added", 0)
            _rq_ignored  = _rq_sync_result.get("ignored_not_opportunity", 0)
            _rq_new      = _rq_sync_result.get("new", 0)
            _rq_upd      = _rq_sync_result.get("updated", 0)
            _rq_nr       = _rq_sync_result.get("needs_review", 0)
            _rq_nh       = _rq_sync_result.get("needs_human", 0)
            _rq_miss     = _rq_sync_result.get("missing_text", 0)
            _rq_skip_new = _rq_sync_result.get("skipped", 0)

            st.success(
                f"Sync complete — "
                f"**{_rq_opp} Instantly opportunity lead(s)** + {_rq_local} local replied. "
                f"{_rq_ignored} generic-replied leads ignored (status=2, not opportunities). "
                f"{_rq_new} new · {_rq_upd} refreshed."
            )
            # Reconciliation warning
            _rq_warn = _rq_sync_result.get("reconciliation_warning")
            if _rq_warn:
                st.warning(_rq_warn)

            _rq_stale = _rq_sync_result.get("stale_archived", 0)
            if _rq_stale:
                st.info(f"Auto-cleanup: {_rq_stale} stale record(s) archived before sync.")

            _opportunity_gap = _rq_sync_result.get("opportunity_gap_detected", False)
            _local_detail = _rq_sync_result.get("local_replied_leads_detail") or []

            if _rq_nr:
                st.success(f"**{_rq_nr} positive reply draft(s) ready for review.**")

            if _opportunity_gap:
                _inst_opp_count = _rq_sync_result.get("instantly_opportunity_count") or 0
                _inst_rep_count = _rq_sync_result.get("instantly_replied_count")
                st.warning(
                    f"**Opportunity gap detected.** "
                    f"Instantly reports {_inst_opp_count} opportunit{'y' if _inst_opp_count == 1 else 'ies'}, "
                    f"but no leads with opportunity status (≥ 3) were found via the Instantly leads API. "
                    f"The API does not appear to expose the opportunity lead IDs."
                )
                st.info(
                    "**Diagnostic summary:**\n"
                    f"- Instantly opportunity count (from analytics): **{_inst_opp_count}**\n"
                    f"- Opportunity leads found via API (status ≥ 3): **{_rq_opp}**\n"
                    f"- Local replied leads found: **{len(_local_detail)}**\n"
                    f"- Instantly replied count: **{_inst_rep_count if _inst_rep_count is not None else 'unknown'}**\n"
                    f"- Reply bodies found: **0**\n\n"
                    "Use **Create Draft from Replied Lead** (tab below) to paste the actual "
                    "reply body from the Instantly dashboard and generate a draft."
                )
                if _local_detail:
                    st.markdown("**Candidate replied leads found locally:**")
                    st.dataframe(
                        pd.DataFrame([
                            {
                                "Name": lr.get("prospect_name") or "—",
                                "Email": lr["email"],
                                "Company": lr.get("company") or "—",
                            }
                            for lr in _local_detail
                        ]),
                        hide_index=True,
                        use_container_width=True,
                    )
            elif _rq_opp > 0 and _rq_miss > 0:
                _inst_opp = _rq_sync_result.get("instantly_opportunity_count")
                _opp_label = f"{_inst_opp}" if _inst_opp is not None else "unknown number of"
                st.info(
                    f"Instantly reports {_opp_label} opportunity/ies, but reply body was not "
                    "available from the current API endpoint (tried /api/v2/emails and "
                    "/api/v2/leads/{{id}}). "
                    f"{_rq_miss} record(s) placed in **Missing reply text** tab. "
                    "Find the actual reply in Instantly and use **Create Draft from Replied Lead**."
                )
            if _rq_skip_new:
                st.info(f"{_rq_skip_new} reply(ies) classified as OOO/unsubscribe/negative — skipped.")
            if _rq_nh:
                st.warning(f"{_rq_nh} reply(ies) routed to **Needs human review** (angry/sensitive).")

            _rq_debug_src = _rq_sync_result.get("debug_text_sources") or []
            _rq_debug_skip = _rq_sync_result.get("debug_skipped_signals") or []
            _rq_local_detail_debug = _rq_sync_result.get("local_replied_leads_detail") or []
            with st.expander(
                f"Sync debug — {_rq_total} scanned · {_rq_opp + _rq_local} candidates "
                f"· {_rq_nr} positive drafts · {_rq_miss} missing text · {_rq_ignored} ignored"
            ):
                # Debug summary
                _inst_r = _rq_sync_result.get("instantly_replied_count")
                _inst_o = _rq_sync_result.get("instantly_opportunity_count")
                _aq = _rq_sync_result.get("active_queue_count", "?")
                _gap = _rq_sync_result.get("opportunity_gap_detected", False)
                st.dataframe(pd.DataFrame([
                    {"Metric": "Instantly replied count (from latest snapshot)", "Value": _inst_r if _inst_r is not None else "unknown"},
                    {"Metric": "Instantly opportunity count", "Value": _inst_o if _inst_o is not None else "unknown"},
                    {"Metric": "Opportunity gap detected", "Value": "YES — Instantly reports opportunities but API returned 0 leads with status ≥ 3" if _gap else "No"},
                    {"Metric": "Leads scanned from Instantly", "Value": _rq_total},
                    {"Metric": "Opportunity leads found (status ≥ 3)", "Value": _rq_opp},
                    {"Metric": "Opportunity lead IDs found", "Value": _rq_opp},
                    {"Metric": "Reply bodies found", "Value": _rq_nr + _rq_nh},
                    {"Metric": "Local replied leads added as candidates", "Value": _rq_local},
                    {"Metric": "Generic replied leads ignored (status=2)", "Value": _rq_ignored},
                    {"Metric": "Positive drafts created", "Value": _rq_nr},
                    {"Metric": "Missing reply text records", "Value": _rq_miss},
                    {"Metric": "Needs human", "Value": _rq_nh},
                    {"Metric": "Stale records auto-archived", "Value": _rq_stale},
                    {"Metric": "Active queue count after sync", "Value": _aq},
                ]), hide_index=True)
                st.markdown("**Opportunity filter:** `status ∈ {3,4,5,6}` or explicit interest/opportunity flags. `status=2` excluded.")
                if _rq_local_detail_debug:
                    st.markdown(f"**Local replied leads ({len(_rq_local_detail_debug)}) used as candidates:**")
                    st.dataframe(
                        pd.DataFrame([
                            {
                                "Name": lr.get("prospect_name") or "—",
                                "Email": lr["email"],
                                "Company": lr.get("company") or "—",
                            }
                            for lr in _rq_local_detail_debug
                        ]),
                        hide_index=True,
                    )
                if _rq_debug_src:
                    st.markdown(f"**Reply body fetch — {len(_rq_debug_src)} candidate(s) probed:**")
                    for _ds in _rq_debug_src[:10]:
                        st.json({k: v for k, v in _ds.items()})
                if _rq_debug_skip:
                    st.markdown(f"**Sample skipped leads (no opportunity signal, {len(_rq_debug_skip)} shown):**")
                    st.dataframe(pd.DataFrame(_rq_debug_skip), hide_index=True)
            st.rerun()

    # ── Summary KPIs + Cleanup ───────────────────────────────────────────
    try:
        _rq_threads = get_reply_queue(workspace_id=_ws_id)
    except Exception as _rq_load_exc:
        _rq_err_str = str(_rq_load_exc)
        if "undefined" in _rq_err_str.lower() or "does not exist" in _rq_err_str.lower():
            st.warning(
                "Reply Agent tables are initializing. "
                "Reboot or refresh the page in a few seconds to continue."
            )
        else:
            st.error(f"Could not load reply queue: {_rq_load_exc}")
        _rq_threads = []

    # KPI cards count only ACTIVE records (excludes archived and skipped)
    from collections import Counter as _Counter
    _rq_status_counts = _Counter(t["status"] for t in _rq_threads)

    # Total records in DB (including archived) for cleanup button visibility
    _rq_total_all = len(_rq_threads)

    _k1, _k2, _k3, _k4, _k5 = st.columns(5)
    with _k1:
        kpi_card("Positive drafts", str(_rq_status_counts.get(STATUS_NEEDS_REVIEW, 0)))
    with _k2:
        kpi_card("Needs human", str(_rq_status_counts.get(STATUS_NEEDS_HUMAN, 0)))
    with _k3:
        kpi_card("Missing reply text", str(_rq_status_counts.get(STATUS_MISSING_TEXT, 0)))
    with _k4:
        kpi_card("Sent", str(_rq_status_counts.get(STATUS_SENT, 0)))
    with _k5:
        kpi_card(
            "Manual / Failed",
            str(_rq_status_counts.get(STATUS_MANUAL_SEND, 0) + _rq_status_counts.get("failed", 0)),
        )

    # Cleanup expander — always visible, not gated on record count
    with st.expander("Cleanup stale Reply Agent records"):
        st.caption(
            "Use these to clean up records from earlier broken sync runs. "
            "`Archive all stale records` is the recommended first step — "
            "it removes all placeholder/wrong-campaign records in one click."
        )
        _cc0, _cc1, _cc2, _cc3 = st.columns(4)
        with _cc0:
            if st.button("Preview (dry run)", key="rq_preview_cleanup"):
                _ps = archive_stale_threads(_ws_id, campaign_id=None, dry_run=True)
                _p1 = archive_placeholder_threads(_ws_id, dry_run=True)
                _p2 = archive_non_opportunity_threads(_ws_id, dry_run=True)
                st.info(
                    f"**Archive all stale** would archive: {_ps['archived']} of {_ps['total_before']} total\n\n"
                    f"**Placeholder only**: {_p1['archived']}\n\n"
                    f"**Non-opportunity only**: {_p2['archived']}\n\n"
                    f"Real records preserved: {_ps['preserved']}"
                )
        with _cc1:
            if st.button(
                "Archive all stale records",
                key="rq_archive_all_stale",
                type="primary",
                help="Archives all placeholder, wrong-campaign, and no-body records. Recommended cleanup.",
            ):
                _cr_stale = archive_stale_threads(_ws_id, campaign_id=None, dry_run=False)
                st.success(
                    f"Archived {_cr_stale['archived']} stale records. "
                    f"{_cr_stale['preserved']} real records preserved. "
                    f"Active queue: {_cr_stale['active_after']}."
                )
                st.cache_data.clear()
                st.rerun()
        with _cc2:
            if st.button(
                "Archive placeholders",
                key="rq_clean_placeholders",
                type="secondary",
                help="Archives records with no reply body (placeholder text).",
            ):
                _cr = archive_placeholder_threads(_ws_id, dry_run=False)
                st.success(f"Archived {_cr['archived']} placeholder records.")
                st.cache_data.clear()
                st.rerun()
        with _cc3:
            if st.button(
                "Archive non-opportunity",
                key="rq_clean_nonopp",
                type="secondary",
                help="Archives records whose Instantly payload has no opportunity signal.",
            ):
                _cr2 = archive_non_opportunity_threads(_ws_id, dry_run=False)
                st.success(f"Archived {_cr2['archived']} non-opportunity records.")
                st.cache_data.clear()
                st.rerun()

    # ── Reply list — tabbed by bucket ────────────────────────────────────
    _rq_positive   = [t for t in _rq_threads if t.get("status") == STATUS_NEEDS_REVIEW]
    _rq_human      = [t for t in _rq_threads if t.get("status") == STATUS_NEEDS_HUMAN]
    _rq_missing    = [t for t in _rq_threads if t.get("status") == STATUS_MISSING_TEXT]
    _rq_sent_list  = [t for t in _rq_threads if t.get("status") == STATUS_SENT]

    _bt_pos = f"Positive drafts ({len(_rq_positive)})"
    _bt_hum = f"Needs human ({len(_rq_human)})"
    _bt_mis = f"Missing reply text ({len(_rq_missing)})"
    _bt_snt = f"Sent ({len(_rq_sent_list)})"
    _rq_bucket_tab, _rq_human_tab, _rq_miss_tab, _rq_sent_tab = st.tabs([_bt_pos, _bt_hum, _bt_mis, _bt_snt])

    def _render_reply_detail(thread: dict, key_prefix: str) -> None:
        """Render the detail panel for a selected reply thread."""
        tid = thread["id"]
        with st.container(border=True):
            _h1, _h2, _h3 = st.columns(3)
            with _h1:
                st.markdown(f"**Prospect:** {thread.get('prospect_name') or '—'} `{thread.get('prospect_email')}`")
            with _h2:
                st.markdown(f"**Company:** {thread.get('company_name') or '—'}")
            with _h3:
                st.markdown(f"**Status:** {_RA_STATUS_ICONS.get(thread['status'], thread['status'])}")

            is_placeholder = (thread.get("inbound_reply_text") or "").startswith("[Reply text not available")
            if is_placeholder:
                st.warning(
                    "Instantly reports this lead as replied/opportunity, but the reply body "
                    "was not available from the current API endpoint. "
                    "Use the **Manual Draft Tester** tab to paste the reply and generate a draft."
                )
            else:
                st.markdown("**Inbound reply:**")
                st.text_area(
                    "Inbound reply", value=thread["inbound_reply_text"],
                    height=100, disabled=True,
                    key=f"{key_prefix}_inbound_{tid}", label_visibility="collapsed",
                )

            if thread.get("original_outbound_email"):
                with st.expander("Original outbound email"):
                    st.text(thread["original_outbound_email"])

            if thread.get("human_review_notes"):
                st.warning(f"**Review notes:** {thread['human_review_notes']}")

            _il, _ir = st.columns(2)
            with _il:
                st.caption(f"Intent: `{thread.get('classification') or '—'}`")
            with _ir:
                st.caption(f"Recommended: {_RA_ACTION_LABELS.get(thread.get('recommended_action') or '', thread.get('recommended_action') or '—')}")

            if not is_placeholder:
                st.markdown("**Reply draft** — edit before approving:")
                draft_key = f"{key_prefix}_draft_{tid}"
                edited_draft = st.text_area(
                    "Draft",
                    value=st.session_state.get(draft_key, thread["draft_body"]),
                    height=150, key=draft_key, label_visibility="collapsed",
                    help="Edit the draft before approving.",
                )

                send_blocked = get_send_blocked_reason(thread)
                is_terminal = thread["status"] in (STATUS_SENT, STATUS_SKIPPED)

                _ba, _bb, _bc = st.columns(3)
                with _ba:
                    approve = st.button(
                        "Approve and send", type="primary",
                        key=f"{key_prefix}_approve_{tid}",
                        disabled=bool(send_blocked or is_terminal),
                        help=send_blocked or ("Already terminal." if is_terminal else None),
                    )
                with _bb:
                    skip_btn = st.button("Skip", type="secondary", key=f"{key_prefix}_skip_{tid}", disabled=is_terminal)
                with _bc:
                    regen_btn = st.button("Regenerate draft", type="secondary", key=f"{key_prefix}_regen_{tid}", disabled=is_terminal)

                if approve and not send_blocked:
                    with st.spinner("Sending via Instantly…"):
                        sr = run_async(try_send_reply(int(tid), workspace_id=_ws_id, draft_override=edited_draft or None))
                    if sr["status"] == STATUS_SENT:
                        st.success("Reply sent.")
                    elif sr["status"] == STATUS_MANUAL_SEND:
                        st.warning(sr["detail"])
                        if sr.get("debug"):
                            with st.expander("Send debug"):
                                st.json(sr["debug"])
                    else:
                        st.error(f"Send failed: {sr['detail']}")
                    st.cache_data.clear()
                    st.rerun()

                if skip_btn:
                    update_reply_thread(int(tid), status=STATUS_SKIPPED)
                    st.cache_data.clear()
                    st.rerun()

                if regen_btn:
                    with st.spinner("Regenerating draft…"):
                        try:
                            rr = run_async(classify_and_draft_reply(
                                inbound_reply=thread["inbound_reply_text"],
                                original_outbound_email=thread.get("original_outbound_email") or "",
                                calendar_link=_ws_calendar_link,
                                workspace_id=_ws_id,
                            ))
                            update_reply_thread(
                                int(tid),
                                classification=rr.classification,
                                recommended_action=rr.recommended_action,
                                draft_body=rr.draft_body,
                                human_review_notes=rr.human_review_notes,
                            )
                            st.session_state.pop(draft_key, None)
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as _re:
                            st.error(f"Regenerate failed: {_re}")

    def _render_bucket_tab(bucket_threads: list[dict], tbl_key: str) -> None:
        if not bucket_threads:
            st.info("Nothing here yet.")
            return
        st.caption(f"{len(bucket_threads)} record(s) · opportunity replies only")
        rows = []
        for _t in bucket_threads:
            _recv = _t.get("reply_received_at")
            rows.append({
                "id": _t["id"],
                "Prospect": _t.get("prospect_name") or _t.get("prospect_email") or "—",
                "Company": _t.get("company_name") or "—",
                "Received": _recv.strftime("%Y-%m-%d %H:%M") if _recv else "—",
                "Classification": _t.get("classification") or "—",
                "Status": _RA_STATUS_ICONS.get(_t.get("status") or "", _t.get("status") or "—"),
                "Snippet": (_t.get("inbound_reply_text") or "")[:60].replace("\n", " ").strip() + "…",
            })
        df = pd.DataFrame.from_records(rows, columns=["id", "Prospect", "Company", "Received", "Classification", "Status", "Snippet"])
        sel = st.dataframe(df, hide_index=True, use_container_width=True, selection_mode="single-row", on_select="rerun", key=tbl_key)
        sel_rows = sel.selection.rows if sel and sel.selection else []
        if not sel_rows:
            st.caption("Select a row to review.")
        else:
            chosen = next((t for t in bucket_threads if t["id"] == df.iloc[sel_rows[0]]["id"]), None)
            if chosen:
                _render_reply_detail(chosen, tbl_key)

    with _rq_bucket_tab:
        _render_bucket_tab(_rq_positive, "rq_pos")
    with _rq_human_tab:
        _render_bucket_tab(_rq_human, "rq_hum")
    with _rq_miss_tab:
        if not _rq_missing:
            st.info("No missing-text records. Good.")
        else:
            st.info(
                f"{len(_rq_missing)} lead(s) were identified as replied/opportunity but "
                "the reply body could not be retrieved from Instantly's API. "
                "To generate a draft for these: find the actual reply in the Instantly dashboard, "
                "copy it, and paste it into the **Manual Draft Tester** tab."
            )
            for _mt in _rq_missing:
                st.caption(
                    f"**{_mt.get('prospect_name') or _mt.get('prospect_email')}** "
                    f"· {_mt.get('company_name') or '—'} "
                    f"· Opportunity signal: {(_mt.get('dedup_key') or '?').replace('lead:', '')}"
                )
    with _rq_sent_tab:
        _render_bucket_tab(_rq_sent_list, "rq_snt")

# ── Tab 2: Manual Draft Tester ────────────────────────────────────────────
with _tab_manual:
    st.caption(
        "Paste a reply manually to test the draft agent. "
        "This does not sync from Instantly and does not send anything."
    )

    with st.form("reply_agent_form", clear_on_submit=False):
        _ra_col1, _ra_col2 = st.columns(2)
        with _ra_col1:
            _ra_inbound = st.text_area(
                "Paste inbound reply",
                placeholder="Paste the prospect reply here",
                height=120,
                key="ra_inbound",
            )
            _ra_original = st.text_area(
                "Original outbound email",
                placeholder="Paste the original email if available",
                height=80,
                key="ra_original",
            )
        with _ra_col2:
            _ra_context = st.text_area(
                "Lead or company context",
                placeholder="Add anything useful about the lead, company, offer, or ICP",
                height=80,
                key="ra_context",
            )
            _ra_calendar = st.text_input(
                "Calendar link",
                placeholder="Paste booking link (or leave blank to use workspace default)",
                key="ra_calendar",
                value=_ws_calendar_link,
            )
        _ra_submit = st.form_submit_button("Generate reply draft", type="primary")

    if _ra_submit:
        if not _ra_inbound.strip():
            st.warning("Paste an inbound reply to continue.")
        else:
            with st.spinner("Classifying and drafting reply…"):
                try:
                    _ra_result = run_async(classify_and_draft_reply(
                        inbound_reply=_ra_inbound,
                        original_outbound_email=_ra_original,
                        lead_context=_ra_context,
                        calendar_link=_ra_calendar or _ws_calendar_link,
                        workspace_id=_ws_id,
                    ))
                    st.session_state["ra_last_result"] = {
                        "result": _ra_result,
                        "inbound_snippet": _ra_inbound[:80],
                    }
                    _ra_history = st.session_state.get("ra_history", [])
                    _ra_history.insert(0, {
                        "result": _ra_result,
                        "inbound_snippet": _ra_inbound[:80],
                    })
                    st.session_state["ra_history"] = _ra_history[:10]
                    save_reply_draft(
                        _ra_result,
                        inbound_reply=_ra_inbound,
                        original_outbound_email=_ra_original,
                        lead_context=_ra_context,
                        workspace_id=_ws_id,
                    )
                except Exception as _ra_exc:
                    st.error(f"Reply Agent failed: {_ra_exc}")
                    st.session_state.pop("ra_last_result", None)

    if "ra_last_result" in st.session_state:
        _ra_entry = st.session_state["ra_last_result"]
        _ra_r = _ra_entry["result"]
        with st.container(border=True):
            _ra_action_label = _RA_ACTION_LABELS.get(_ra_r.recommended_action, _ra_r.recommended_action)
            _ra_left, _ra_right = st.columns(2)
            with _ra_left:
                st.markdown(f"**Intent:** `{_ra_r.classification}`")
            with _ra_right:
                st.markdown(f"**Recommended action:** {_ra_action_label}")
            if _ra_r.human_review_notes:
                st.info(f"**Review notes:** {_ra_r.human_review_notes}")
            st.markdown("**Suggested reply draft** — copy manually before sending:")
            st.text_area(
                "Draft",
                value=_ra_r.draft_body,
                height=150,
                key="ra_draft_output",
                label_visibility="collapsed",
            )

    if st.session_state.get("ra_history"):
        with st.expander(
            f"Draft history — this session ({len(st.session_state['ra_history'])} draft(s))"
        ):
            for _ra_i, _ra_h in enumerate(st.session_state["ra_history"]):
                _r = _ra_h["result"]
                with st.container(border=True):
                    st.caption(
                        f"#{_ra_i + 1} · {_r.classification} → {_r.recommended_action} "
                        f"· {_ra_h['inbound_snippet']}…"
                    )
                    st.text(_r.draft_body[:300])

# ── Tab 3: Create Draft from Replied Lead ────────────────────────────────
with _tab_create:
    st.caption(
        "Select a replied lead from this workspace, paste the actual reply text "
        "from the Instantly dashboard, and generate a draft tied to that lead. "
        "Does not auto-send. The draft is saved to the Reply Queue for review."
    )

    _cdf_local_replied = get_local_replied_leads(workspace_id=_ws_id)

    if not _cdf_local_replied:
        st.info(
            "No local replied leads found for this workspace. "
            "Replied leads appear here after syncing engagement from Instantly. "
            "You can also use the **Manual Draft Tester** tab to generate a draft "
            "without selecting a lead."
        )
    else:
        _cdf_lead_options: dict[str, str] = {
            lr["email"]: (
                f"{lr.get('prospect_name') or lr['email']} "
                f"· {lr.get('company') or '—'} "
                f"· {lr['email']}"
            )
            for lr in _cdf_local_replied
        }

        with st.form("create_draft_from_replied_lead_form", clear_on_submit=False):
            _cdf_email = st.selectbox(
                "Select replied lead",
                options=list(_cdf_lead_options.keys()),
                format_func=lambda e: _cdf_lead_options[e],
                key="cdf_lead_select",
                index=None,
                placeholder="Choose a replied lead…",
            )
            _cdf_reply = st.text_area(
                "Paste reply body from Instantly dashboard",
                placeholder=(
                    "I'm happy to pay you 25% bounty on every one that buys. "
                    "Our cycle is measured in weeks, not months or quarters."
                ),
                height=120,
                key="cdf_reply",
            )
            _cdf_calendar = st.text_input(
                "Calendar link",
                value=_ws_calendar_link,
                key="cdf_calendar",
            )
            _cdf_submit = st.form_submit_button("Generate draft", type="primary")

        if _cdf_submit:
            if not _cdf_email:
                st.warning("Select a replied lead to continue.")
            elif not _cdf_reply.strip():
                st.warning("Paste the reply text to continue.")
            else:
                _cdf_selected_lr = next(
                    (lr for lr in _cdf_local_replied if lr["email"] == _cdf_email), None
                )
                with st.spinner("Classifying and drafting reply…"):
                    try:
                        _cdf_thread_id, _cdf_result = run_async(create_draft_from_replied_lead(
                            prospect_email=_cdf_email,
                            reply_text=_cdf_reply,
                            workspace_id=_ws_id,
                            lead_id=_cdf_selected_lr.get("lead_db_id") if _cdf_selected_lr else None,
                            prospect_name=_cdf_selected_lr.get("prospect_name") if _cdf_selected_lr else None,
                            company_name=_cdf_selected_lr.get("company") if _cdf_selected_lr else None,
                            calendar_link=_cdf_calendar or _ws_calendar_link,
                        ))
                        st.session_state["cdf_last_result"] = {
                            "result": _cdf_result,
                            "thread_id": _cdf_thread_id,
                            "email": _cdf_email,
                            "lead_name": (
                                _cdf_selected_lr.get("prospect_name") if _cdf_selected_lr else _cdf_email
                            ),
                        }
                        st.cache_data.clear()
                        st.success(f"Draft created and saved to Reply Queue (thread #{_cdf_thread_id}).")
                    except Exception as _cdf_exc:
                        st.error(f"Create draft failed: {_cdf_exc}")

        if "cdf_last_result" in st.session_state:
            _cdf_entry = st.session_state["cdf_last_result"]
            _cdf_r = _cdf_entry["result"]
            with st.container(border=True):
                st.markdown(
                    f"**Lead:** {_cdf_entry['lead_name']} (`{_cdf_entry['email']}`)"
                )
                _cdf_action_label = _RA_ACTION_LABELS.get(
                    _cdf_r.get("recommended_action", ""), _cdf_r.get("recommended_action", "—")
                )
                _cdf_left, _cdf_right = st.columns(2)
                with _cdf_left:
                    st.markdown(f"**Intent:** `{_cdf_r.get('classification', '—')}`")
                with _cdf_right:
                    st.markdown(f"**Recommended action:** {_cdf_action_label}")
                if _cdf_r.get("human_review_notes"):
                    st.info(f"**Review notes:** {_cdf_r['human_review_notes']}")
                st.markdown("**Suggested reply draft** — saved to queue for your review:")
                st.text_area(
                    "Draft",
                    value=_cdf_r.get("draft_body", ""),
                    height=150,
                    key="cdf_draft_output",
                    label_visibility="collapsed",
                )
                st.caption(
                    f"Saved as Reply Queue thread #{_cdf_entry['thread_id']}. "
                    "Review and approve it in the **Reply Queue** tab."
                )

with st.expander("Sync debug — raw Instantly analytics + DB comparison"):
    if _snapshot is None:
        st.info("No analytics snapshot yet. Hit \"Sync engagement from Instantly\".")
    else:
        _raw = _snapshot.get("raw") or {}
        _selected_id = (
            _raw.get("campaign_id")
            or _raw.get("id")
            or _raw.get("campaign")
            or _snapshot.get("campaign_id")
        )
        _selected_name = _raw.get("campaign_name") or _raw.get("name") or "—"
        st.markdown(
            f"**Requested campaign ID (env):** `{_snapshot.get('campaign_id')}`  \n"
            f"**Selected campaign ID (from Instantly):** `{_selected_id}`  \n"
            f"**Selected campaign name:** {_selected_name}  \n"
            f"**Last synced:** {_format_timestamp(_snapshot.get('synced_at'))}"
        )
        if str(_selected_id).lower() != str(_snapshot.get("campaign_id")).lower():
            st.warning(
                "Selected campaign id does not match the configured "
                "INSTANTLY_CAMPAIGN_ID. The snapshot may be stale — re-sync."
            )

        # ---- Open rate breakdown ----
        st.markdown("**Open rate breakdown**")
        _or_source = _metrics.get("open_rate_source", "—")
        _or_pct = _metrics.get("open_rate", 0.0) * 100
        _total_opens = _metrics.get("opens", 0)
        _uopens = _metrics.get("unique_opens")
        st.dataframe(
            pd.DataFrame([
                {"field": "total_opens (open_count)", "value": _total_opens},
                {"field": "unique_opens (unique_open_count)", "value": "—" if _uopens is None else _uopens},
                {"field": "contacted / sequence_started", "value": contacted},
                {"field": "emails_sent_count", "value": sent_remote},
                {"field": "formula used", "value": _or_source},
                {"field": "displayed open rate", "value": f"{_or_pct:.2f}%"},
                {"field": "Instantly UI open rate (expected)", "value": "39.06% (unique_opens/contacted)"},
            ]),
            hide_index=True,
            use_container_width=True,
        )

        # ---- Positive reply / opportunity attribution ----
        st.markdown("**Positive reply / opportunity source**")
        _pos_reply_src = _snapshot.get("raw_positive_reply_source") or "—"
        _opp_src = _snapshot.get("raw_opportunity_source") or "—"
        st.dataframe(
            pd.DataFrame([
                {"field": "positive_reply_count (stored)", "value": _snapshot.get("positive_reply_count", "—")},
                {"field": "opportunity_count (stored)", "value": _snapshot.get("opportunity_count", "—")},
                {"field": "raw_positive_reply_source", "value": _pos_reply_src},
                {"field": "raw_opportunity_source", "value": _opp_src},
                {"field": "positive_signals (displayed KPI)", "value": positive_signals},
            ]),
            hide_index=True,
            use_container_width=True,
        )

        # ---- Recursive positive-keyword search ----
        st.markdown("**Positive-keyword field search (raw analytics JSON)**")
        _pos_hits = search_positive_fields(_raw)
        if _pos_hits:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Raw key": k,
                        "Raw value": v,
                        "Normalized metric target": (
                            "positive_reply_count" if "positive" in k.lower() and "reply" in k.lower()
                            else "opportunity_count" if "opportunit" in k.lower()
                            else "reply_count" if "reply" in k.lower() or "repli" in k.lower()
                            else "status" if "status" in k.lower()
                            else "conversion_count" if "convers" in k.lower()
                            else "(informational)"
                        ),
                    }
                    for k, v in sorted(_pos_hits.items())
                ]),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption(
                "No positive-engagement keys found in the raw analytics JSON. "
                "Instantly may not expose opportunity count via the analytics endpoint — "
                "the leads fallback (iterating per-lead statuses) is the source."
            )

        # ---- Full raw analytics JSON fields (all keys) ----
        st.markdown("**All raw Instantly analytics keys**")
        if _raw:
            st.dataframe(
                pd.DataFrame([{"field": k, "raw value": v} for k, v in sorted(_raw.items())]),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption("No raw response available.")

        # ---- Metric · Instantly raw · Stored snapshot · Displayed KPI ----
        st.markdown("**Metric comparison table**")
        st.dataframe(
            pd.DataFrame([
                {"Metric": "Sequence started",      "Instantly raw": _raw.get("contacted_count", "—"),      "Stored snapshot": _snapshot.get("contacted_count", "—"),      "Displayed KPI": contacted},
                {"Metric": "Emails sent (total)",   "Instantly raw": _raw.get("emails_sent_count", "—"),    "Stored snapshot": _snapshot.get("emails_sent_count", "—"),    "Displayed KPI": sent_remote},
                {"Metric": "Total opens",            "Instantly raw": _raw.get("open_count", "—"),           "Stored snapshot": _snapshot.get("open_count", "—"),           "Displayed KPI": _total_opens},
                {"Metric": "Unique opens",           "Instantly raw": _raw.get("unique_opened_count") or _raw.get("unique_open_count", "—"), "Stored snapshot": _snapshot.get("unique_open_count", "—"), "Displayed KPI": "—" if _uopens is None else _uopens},
                {"Metric": "Replies (total)",        "Instantly raw": _raw.get("reply_count", "—"),          "Stored snapshot": _snapshot.get("reply_count", "—"),          "Displayed KPI": replies},
                {"Metric": "Positive replies",       "Instantly raw": _raw.get("positive_reply_count", "—"),"Stored snapshot": _snapshot.get("positive_reply_count", "—"), "Displayed KPI": _metrics.get("positive_replies", "—")},
                {"Metric": "Opportunities",          "Instantly raw": _raw.get("opportunity_count", "—"),    "Stored snapshot": _snapshot.get("opportunity_count", "—"),    "Displayed KPI": opportunities},
                {"Metric": "Bounces",                "Instantly raw": _raw.get("bounced_count", "—"),        "Stored snapshot": _snapshot.get("bounced_count", "—"),        "Displayed KPI": bounces},
                {"Metric": "Local sent (DB)",        "Instantly raw": "—",                                   "Stored snapshot": "—",                                        "Displayed KPI": _local_sent},
            ]),
            hide_index=True,
            use_container_width=True,
        )

        # ---- Latest-send source breakdown ----
        st.markdown("**Latest send source**")
        try:
            _send_info = latest_send_info()
        except Exception as exc:
            st.warning(f"Could not resolve latest-send info: {exc}")
            _send_info = None
        if _send_info is not None:
            _instantly_ts = _send_info.get("latest_instantly_send_at")
            _local_ts = _send_info.get("latest_local_delivery_send_at")
            _chosen_ts = _send_info.get("timestamp")
            _source = _send_info.get("source") or "—"
            _hours = (
                None if _chosen_ts is None
                else max(0.0, (datetime.utcnow() - _chosen_ts).total_seconds() / 3600.0)
            )
            st.dataframe(
                pd.DataFrame([
                    {"field": "latest_instantly_send_at",      "value": _format_timestamp(_instantly_ts)},
                    {"field": "latest_local_delivery_send_at", "value": _format_timestamp(_local_ts)},
                    {"field": "latest_send_source_used",       "value": _source},
                    {"field": "hours_since_latest_send",       "value": "—" if _hours is None else f"{_hours:.1f}h"},
                ]),
                hide_index=True,
                use_container_width=True,
            )

st.divider()

# ---------- Self-improving loop ----------
st.subheader("Self improving loop")
st.caption(
    "Diagnoses the worst-performing metric against configurable benchmarks "
    "and proposes a change. Nothing is applied without your approval. "
    "Approved prompt changes only affect FUTURE generated email content — "
    "already-sent emails are untouched."
)

with st.expander("Benchmarks", expanded=False):
    b1, b2, b3 = st.columns(3)
    with b1:
        open_rate_target = st.number_input(
            "Open rate target (%)", min_value=1.0, max_value=80.0,
            value=DEFAULT_OPEN_RATE_TARGET * 100, step=1.0,
            key="sil_open_target",
        ) / 100.0
    with b2:
        reply_rate_target = st.number_input(
            "Reply rate target (%)", min_value=0.5, max_value=20.0,
            value=DEFAULT_REPLY_RATE_TARGET * 100, step=0.5,
            key="sil_reply_target",
        ) / 100.0
    with b3:
        bounce_rate_max = st.number_input(
            "Bounce rate max (%)", min_value=0.5, max_value=20.0,
            value=DEFAULT_BOUNCE_RATE_MAX * 100, step=0.5,
            key="sil_bounce_max",
        ) / 100.0

if _snapshot is None:
    st.info("Sync from Instantly first — the loop needs a metrics snapshot to diagnose.")
else:
    # `diagnose()` reads the SAME `_metrics` dict the KPI cards above
    # rendered from, so the loop's "current bounce rate" can't differ
    # from the bounce-rate card by a rounding step.
    diag = diagnose(
        _metrics,
        open_rate_target=open_rate_target,
        reply_rate_target=reply_rate_target,
        bounce_rate_max=bounce_rate_max,
        local_sent_count=_local_sent or None,
    )

    _STATUS_BADGE = {
        LOOP_WAIT: ("⏸ Wait", "info"),
        LOOP_DIAGNOSE: ("🔍 Diagnose only", "info"),
        LOOP_DRAFT: ("📝 Draft recommendation", "warning"),
        LOOP_READY: ("✅ Ready for approval", "success"),
    }
    badge_text, badge_kind = _STATUS_BADGE.get(diag.loop_status, ("—", "info"))

    with st.container(border=True):
        # Status banner
        if badge_kind == "success":
            st.success(badge_text)
        elif badge_kind == "warning":
            st.warning(badge_text)
        else:
            st.info(badge_text)

        # Metric panel — same numbers as the KPI cards above.
        cols = st.columns(4)
        with cols[0]:
            st.metric(
                diag.current_metric_label,
                (
                    f"{diag.current_metric_value * 100:.1f}%"
                    if diag.current_metric_label in ("Open rate", "Reply rate", "Bounce rate")
                    else f"{diag.current_metric_value:,.0f}"
                ),
            )
        with cols[1]:
            st.metric(
                "Target",
                (
                    f"{diag.target_metric_value * 100:.1f}%"
                    if diag.current_metric_label in ("Open rate", "Reply rate", "Bounce rate")
                    else f"{diag.target_metric_value:,.0f}"
                ),
            )
        with cols[2]:
            st.metric("Sample", f"{diag.sample_size:,}")
        with cols[3]:
            hours = diag.hours_since_latest_send
            st.metric(
                "Since latest send",
                "—" if hours is None else f"{hours:.1f}h",
            )

        # Show positive_reply_rate alongside the main metric when available.
        if positive_signals > 0 and sent_remote > 0:
            _pos_rate_pct = positive_signals / sent_remote * 100
            st.caption(
                f"Positive reply rate (opportunities): "
                f"{_pos_rate_pct:.2f}% ({positive_signals} of {sent_remote:,} sent)"
            )

        st.markdown(f"**Bottleneck:** `{diag.bottleneck}` · **Confidence:** {diag.confidence}")
        st.markdown(f"**Diagnosis:** {diag.diagnosis}")
        st.markdown(f"**Recommended action:** {diag.recommended_change}")
        if diag.expected_impact and diag.expected_impact != "—":
            st.markdown(f"**Expected impact:** {diag.expected_impact}  ·  **Risk:** {diag.risk_level}")

        # Approval controls only render for prompt-change candidates that
        # passed every gate (sample + timing + open-rate-healthy when
        # applicable). Bounce / delivery / wait / diagnose-only diagnoses
        # NEVER show Approve.
        if diag.is_actionable_prompt_change:
            with st.expander("Proposed addendum — small, appended to current overlay"):
                st.caption(
                    "On approval, this text is APPENDED to the existing email "
                    "prompt overlay. Your previous edits are preserved. Only "
                    "future generated emails are affected."
                )
                st.code(diag.proposed_addendum, language="markdown")

            # Persist the recommendation ONCE per (bottleneck, value)
            # signature so re-rendering the page doesn't spam the history.
            # save_recommendation() returns None on a DB error; in that
            # case we still render the diagnosis read-only with a warning
            # — never crash the whole page.
            sig = f"{diag.bottleneck}_{diag.loop_status}_{int(diag.current_metric_value * 10000)}"
            pending_rec_key = f"sil_pending_rec_{sig}"
            if pending_rec_key not in st.session_state:
                st.session_state[pending_rec_key] = save_recommendation(
                    diag,
                    metric_snapshot=_metrics,
                    snapshot_id=_snapshot.get("id") if _snapshot else None,
                    workspace_id=_ws_id,
                )
            rec_id = st.session_state[pending_rec_key]

            if rec_id is None:
                st.warning(
                    "Could not save this recommendation to the database — "
                    "the diagnosis above is read-only. Check the app logs "
                    "for the SQLAlchemy error; the page itself is fine."
                )
            else:
                a, b, c = st.columns(3)
                with a:
                    approve = st.button(
                        "Approve change for next send",
                        key=f"sil_approve_{rec_id}",
                        type="primary",
                        disabled=(diag.loop_status != LOOP_READY),
                        help=(
                            None
                            if diag.loop_status == LOOP_READY
                            else "Approve is only available at standard confidence "
                            f"(≥ {SAMPLE_LOW_CONF_MAX} sent). Use Save-as-draft "
                            "for low-confidence drafts."
                        ),
                    )
                with b:
                    reject = st.button("Reject", key=f"sil_reject_{rec_id}", type="secondary")
                with c:
                    draft = st.button(
                        "Save as draft recommendation",
                        key=f"sil_draft_{rec_id}",
                        type="secondary",
                    )

                if approve and diag.loop_status == LOOP_READY:
                    try:
                        approve_recommendation(rec_id, approved_by="demo_sdr", workspace_id=_ws_id)
                    except Exception as exc:
                        st.error(f"Approval failed: {exc}")
                    else:
                        st.cache_data.clear()
                        st.session_state.pop(pending_rec_key, None)
                        st.success(
                            "Approved — addendum appended to the email "
                            "prompt overlay. Future generations will use "
                            "it; already-sent emails are unchanged."
                        )
                        st.rerun()
                if reject:
                    try:
                        reject_recommendation(rec_id, rejected_by="demo_sdr")
                    except Exception as exc:
                        st.error(f"Reject failed: {exc}")
                    else:
                        st.session_state.pop(pending_rec_key, None)
                        st.cache_data.clear()
                        st.info("Recommendation rejected.")
                        st.rerun()
                if draft:
                    try:
                        save_as_draft(rec_id)
                    except Exception as exc:
                        st.error(f"Save-as-draft failed: {exc}")
                    else:
                        st.session_state.pop(pending_rec_key, None)
                        st.cache_data.clear()
                        st.info("Saved as draft.")
                        st.rerun()
        elif diag.bottleneck == "none":
            st.caption("Green across the board — nothing to do.")
        else:
            # wait / diagnose-only for any bottleneck, or bounce/delivery.
            # No buttons by design.
            st.caption(
                "No prompt change available at this state. Re-evaluate after "
                "more sends arrive or the timing window passes."
            )

# ---------- Recommendation history ----------
with st.expander("Recommendation history & rollback"):
    recs = _recommendations_cached(workspace_id=_ws_id)

    # Staleness reference values from the current snapshot.
    _current_snap_id = _snapshot.get("id") if _snapshot else None
    _current_sent = _metrics.get("sent", 0)
    _current_snap_ts = _snapshot.get("synced_at") if _snapshot else None
    _current_opps = _metrics.get("positive_signals", 0)

    # "Recalculate from latest data" — clears all pending recommendation keys
    # so the next page render creates a new recommendation from current metrics.
    if _snapshot is not None:
        if st.button(
            "Recalculate recommendation from latest Instantly data",
            key="sil_recalculate",
            help=(
                "Clears cached recommendation and re-diagnoses using the "
                "latest analytics snapshot. Does not sync — hit "
                "'Sync engagement from Instantly' first for fresh data."
            ),
        ):
            for k in list(st.session_state.keys()):
                if k.startswith("sil_pending_rec_"):
                    del st.session_state[k]
            st.cache_data.clear()
            st.rerun()

    if not recs:
        st.caption("No recommendations yet.")
    else:
        for rec in recs:
            # --- Staleness check ---
            rec_snap_id = rec.get("analytics_snapshot_id")
            rec_sent = int(rec.get("sample_size") or 0)

            is_stale = False
            stale_reason = ""
            if rec_snap_id and _current_snap_id and rec_snap_id < _current_snap_id:
                is_stale = True
                stale_reason = "a newer analytics snapshot exists"
            elif rec_sent > 0 and _current_sent > 0:
                drift = abs(_current_sent - rec_sent) / max(rec_sent, 1)
                if drift > 0.10:
                    is_stale = True
                    stale_reason = (
                        f"sent count changed from {rec_sent:,} → {_current_sent:,} "
                        f"({drift * 100:.0f}% drift)"
                    )

            _rec_status = rec["status"]
            status_icon = {
                "approved": "✓",
                "rejected": "✗",
                "draft": "·",
                "ready_for_approval": "⏳",
                "archived": "🗄",
            }.get(_rec_status, "?")

            _is_pending = _rec_status in ("ready_for_approval", "draft")
            if _rec_status == "archived":
                status_label = "Archived"
            elif is_stale and _is_pending:
                status_label = "Stale recommendation"
            else:
                status_label = _rec_status.replace("_", " ").capitalize()

            header = (
                f"{status_icon} {status_label} · "
                f"{rec['bottleneck']} · "
                f"{rec['current_metric_label']} "
                f"{rec['current_metric_value'] * 100:.1f}% → "
                f"target {rec['target_metric_value'] * 100:.1f}%"
            )

            with st.container(border=True):
                st.markdown(header)

                # Snapshot provenance row
                _based_on = f"sample {rec_sent:,}"
                if _current_snap_ts:
                    _based_on += f"  ·  created {_format_timestamp(rec['created_at'])}"
                st.caption(f"Based on: {_based_on}")
                if _current_snap_id:
                    _curr_info = f"sample {_current_sent:,}"
                    if _current_opps:
                        _curr_info += f", {_current_opps} opportunity/ies"
                    st.caption(f"Current snapshot: {_curr_info}  ·  synced {_format_timestamp(_current_snap_ts)}")

                if is_stale and _rec_status != "archived":
                    st.error(
                        f"**Stale:** This recommendation was based on older campaign data "
                        f"({stale_reason}). Do not approve — recalculate or archive it."
                    )

                st.caption(rec["diagnosis"])

                # Addendum preview — label clearly when the rec is stale or archived.
                if rec.get("proposed_addendum"):
                    _addendum_label = (
                        "Historical recommendation — not active (stale, do not approve)"
                        if (is_stale or _rec_status == "archived") and _is_pending or _rec_status == "archived"
                        else "Addendum that was proposed"
                    )
                    with st.expander(_addendum_label):
                        if is_stale and _is_pending:
                            st.warning(
                                "This addendum was drafted from an older snapshot. "
                                "It should not be approved as-is. Archive and recalculate."
                            )
                        st.code(rec["proposed_addendum"], language="markdown")

                if rec["approved_by"]:
                    st.caption(
                        f"By {rec['approved_by']} at "
                        f"{_format_timestamp(rec['approved_at'])}"
                    )
                if (
                    _rec_status == "approved"
                    and rec["previous_prompt_snapshot"]
                    and rec["channel"]
                ):
                    if st.button(
                        "Roll back this change",
                        key=f"sil_rollback_{rec['id']}",
                        type="secondary",
                    ):
                        try:
                            rollback_recommendation(rec["id"], workspace_id=_ws_id)
                        except Exception as exc:
                            st.error(f"Rollback failed: {exc}")
                        else:
                            st.cache_data.clear()
                            st.success("Rolled back to previous prompt overlay.")
                            st.rerun()

                # For stale pending recommendations: archive + recalculate buttons.
                if is_stale and _is_pending:
                    st.caption(
                        ":orange[Approval disabled — this recommendation was based on older data.]"
                    )
                    _arch_col, _recalc_col = st.columns(2)
                    with _arch_col:
                        if st.button(
                            "Archive this recommendation",
                            key=f"sil_archive_{rec['id']}",
                            type="secondary",
                            help="Dismisses this stale recommendation without changing the prompt.",
                        ):
                            try:
                                archive_recommendation(rec["id"])
                            except Exception as exc:
                                st.error(f"Archive failed: {exc}")
                            else:
                                st.cache_data.clear()
                                st.info("Recommendation archived.")
                                st.rerun()
                    with _recalc_col:
                        if st.button(
                            "Recalculate from latest data",
                            key=f"sil_recalc_hist_{rec['id']}",
                            type="secondary",
                            help=(
                                "Clears cached recommendations and re-diagnoses "
                                "from the current analytics snapshot."
                            ),
                        ):
                            for k in list(st.session_state.keys()):
                                if k.startswith("sil_pending_rec_"):
                                    del st.session_state[k]
                            st.cache_data.clear()
                            st.rerun()

# ---------- Prompt experiment tracker ----------
with st.expander("Prompt experiment tracker — performance by prompt version"):
    perf = _perf_by_prompt_cached()
    if not perf:
        st.caption("No engagement-synced emails yet — experiment tracker fills in after the first sync.")
    else:
        df = pd.DataFrame(perf)
        df["open_rate"] = (df["open_rate"] * 100).round(1)
        df["reply_rate"] = (df["reply_rate"] * 100).round(2)
        df["bounce_rate"] = (df["bounce_rate"] * 100).round(2)
        df["confidence"] = df["low_confidence"].map(
            lambda lc: "low" if lc else "standard"
        )
        df = df[
            [
                "prompt_version",
                "prompt_fingerprint",
                "sent",
                "open_rate",
                "reply_rate",
                "bounce_rate",
                "confidence",
            ]
        ].rename(
            columns={
                "prompt_version": "Prompt version",
                "prompt_fingerprint": "Fingerprint",
                "sent": "Sent",
                "open_rate": "Open %",
                "reply_rate": "Reply %",
                "bounce_rate": "Bounce %",
                "confidence": "Confidence",
            }
        )
        st.dataframe(df, hide_index=True, width="stretch")
        st.caption(
            f"Rows with Sent < {MIN_SAMPLE_FOR_RECOMMENDATION} are flagged "
            "low-confidence. The self-improvement loop reads campaign-level "
            "Instantly metrics, not this table, but the same minimum-sample "
            "rule applies before any prompt change is suggested."
        )

st.divider()

# ---------- Reply-rate chart ----------
st.subheader("Reply rate over time")
days = st.number_input(
    "Days", min_value=7, max_value=90, value=30, step=1, key="eng_days",
    help="Window for the reply-rate trend below.",
)
try:
    rr_df = _reply_rate_cached(int(days), workspace_id=_ws_id)
except Exception as exc:
    st.error(f"Could not load reply-rate series: {exc}")
    rr_df = pd.DataFrame()

if rr_df.empty or rr_df.shape[0] < 2:
    st.info(
        "Trend chart appears once engagement data has been synced across "
        "multiple days. Hit \"Sync engagement from Instantly\" above to start."
    )
else:
    chart_df = rr_df.set_index("date")[["reply_rate"]]
    st.line_chart(chart_df, height=260)
    st.caption(f"Daily reply rate over the last {int(days)} days.")

st.divider()


# ---------- Winners library ----------
def _render_library(
    title: str,
    entries: list[dict],
    empty_msg: str,
    key_prefix: str,
    set_active: Callable[[str, bool], bool],
    item_noun: str,
    show_reason_filter: bool = False,
) -> None:
    st.subheader(title)
    filter_cols = st.columns([2, 2, 4]) if show_reason_filter else st.columns([2, 6])
    with filter_cols[0]:
        filter_value = st.selectbox(
            "Content type",
            options=_TYPE_FILTER_OPTIONS,
            index=0,
            key=f"{key_prefix}_type_filter",
        )
    reason_filter = "All"
    if show_reason_filter:
        with filter_cols[1]:
            reason_filter = st.selectbox(
                "Winner reason",
                options=_REASON_FILTER_OPTIONS,
                index=0,
                key=f"{key_prefix}_reason_filter",
            )
    df = _library_dataframe(entries, filter_value, reason_filter)
    if df.empty:
        st.info(empty_msg)
        return
    selection = st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        selection_mode="single-row",
        on_select="rerun",
        key=f"{key_prefix}_table",
    )

    selected_rows = (selection.selection.rows if selection and selection.selection else [])
    if not selected_rows:
        st.caption("Select a row to see the full entry.")
        return
    row_idx = selected_rows[0]
    entry_id = df.iloc[row_idx]["id"]
    entry = next((e for e in entries if e.get("id") == entry_id), None)
    if entry is None:
        st.warning("Could not locate selected entry — try re-syncing.")
        return

    with st.container(border=True):
        subject = _entry_subject(entry)
        if subject:
            st.markdown(f"**Subject:** {subject}")
        body = _entry_body(entry)
        if body:
            st.markdown("**Body:**")
            st.text(body)
        ctx = entry.get("lead_context") or {}
        if isinstance(ctx, dict):
            signal = ctx.get("signal") or ctx.get("title") or ctx.get("industry")
            if signal:
                st.caption(f"Lead context: {signal}")
        ctx_summary = entry.get("lead_context_summary")
        if ctx_summary:
            st.caption(f"Lead context: {ctx_summary}")
        feedback_reason = entry.get("feedback_reason")
        if feedback_reason:
            st.caption(f"Marked negative because: {feedback_reason}")

        # Demote / restore (two-click TTL pattern, mirrors
        # app/pages/3_lead_detail.py:468-533).
        currently_active = bool(entry.get("is_active", True))
        pending_key = f"{key_prefix}_demote_pending_{entry_id}"
        pending_at_key = f"{key_prefix}_demote_pending_at_{entry_id}"

        pending = bool(st.session_state.get(pending_key, False))
        pending_at = float(st.session_state.get(pending_at_key, 0.0))
        if pending and (time.monotonic() - pending_at) > DEMOTE_TTL:
            pending = False
            st.session_state[pending_key] = False
            st.caption(":gray[Previous confirmation expired.]")

        if currently_active:
            if not pending:
                if st.button(
                    f"Demote this {item_noun}",
                    type="secondary",
                    key=f"{key_prefix}_demote_init_{entry_id}",
                    help=(
                        "Marks this entry inactive so future content generation "
                        "skips it. The entry stays in the file for audit; "
                        "you can restore it later."
                    ),
                ):
                    st.session_state[pending_key] = True
                    st.session_state[pending_at_key] = time.monotonic()
                    st.rerun()
            else:
                if st.button(
                    f"⚠️ Click again to confirm — {item_noun} will be skipped by future prompts",
                    type="primary",
                    key=f"{key_prefix}_demote_confirm_{entry_id}",
                ):
                    try:
                        changed = set_active(entry_id, False)
                    except OSError as exc:
                        st.error(f"Demote failed: {exc}")
                    else:
                        st.session_state.pop(pending_key, None)
                        st.session_state.pop(pending_at_key, None)
                        st.cache_data.clear()
                        if changed:
                            st.success(f"{item_noun.capitalize()} demoted.")
                        else:
                            st.info("Already in the requested state — no change.")
                        st.rerun()
        else:
            if st.button(
                f"Restore this {item_noun}",
                type="secondary",
                key=f"{key_prefix}_restore_{entry_id}",
            ):
                try:
                    set_active(entry_id, True)
                except OSError as exc:
                    st.error(f"Restore failed: {exc}")
                else:
                    st.cache_data.clear()
                    st.success(f"{item_noun.capitalize()} restored.")
                    st.rerun()


# ---------- Winners library ----------
st.warning(
    "**Winners Library policy:** Only positive replies, opportunities, conversions, "
    "manual approvals, and seed examples should be active winners. "
    "Emails promoted only because of opens, send count, or generic engagement "
    "do not belong here and will degrade future prompt quality if kept active."
)

_wc1, _wc2 = st.columns([3, 2])
with _wc2:
    if st.button(
        "Cleanup invalid winners",
        key="cleanup_winners_btn",
        help=(
            "Deactivates engagement_reply entries that cannot be confirmed as a "
            "positive reply via the Engagement table. Seed and manual approval "
            "entries are always kept. Entries are NOT deleted — you can restore "
            "any entry individually."
        ),
    ):
        try:
            cleanup_result = cleanup_invalid_winners()
        except Exception as exc:
            st.error(f"Cleanup failed: {exc}")
        else:
            st.cache_data.clear()
            st.success(
                f"Cleanup complete — "
                f"{cleanup_result['kept_active']} kept active, "
                f"{cleanup_result['deactivated']} deactivated, "
                f"{cleanup_result['reason_set']} reasons set."
            )
            st.rerun()

_render_library(
    "Winners library",
    _winners_cached(workspace_id=_ws_id),
    empty_msg="No winners yet for this workspace. Hit \"Promote winners now\" once engagement replies or thumbs-up ratings exist.",
    key_prefix="eng_winners",
    set_active=set_winner_active,
    item_noun="winner",
    show_reason_filter=True,
)

st.divider()

# ---------- Negative examples library ----------
_render_library(
    "Negative examples library",
    _negatives_cached(),
    empty_msg="No negative examples yet. They populate from thumbs-down ratings with written feedback.",
    key_prefix="eng_negatives",
    set_active=set_negative_active,
    item_noun="negative example",
)

st.divider()

# ---------- Recent engagement events ----------
st.subheader("Recent engagement events")
recent_limit = st.number_input(
    "Show last", min_value=5, max_value=200, value=20, step=5, key="eng_recent_limit",
)
try:
    recent_df = _recent_engagement_cached(int(recent_limit), workspace_id=_ws_id)
except Exception as exc:
    st.error(f"Could not load recent engagement: {exc}")
    recent_df = pd.DataFrame()

if recent_df.empty:
    st.info(
        "Engagement events appear here after the first sync from Instantly "
        "(provided you have an INSTANTLY_API_KEY set and at least one email delivered)."
    )
else:
    st.caption("Click a row to see the email that was sent.")
    for _, row in recent_df.iterrows():
        # Row label per spec: sent timestamp · lead name · company · Email · delivery status.
        # Engagement icons (open/click/reply/bounce) appended when present so
        # synced events stay visible without losing the spec-mandated format.
        when = _format_event_when(row.get("delivered_at") or row.get("synced_at"))
        lead = row["lead"] or "(unknown lead)"
        company = row.get("company") or ""
        email = row.get("email") or ""
        delivery_status = row.get("delivery_status") or "—"
        header_bits = [when, lead]
        if company:
            header_bits.append(company)
        if email:
            header_bits.append(email)
        header_bits.append(delivery_status)
        signals = _event_status_icons(row)
        if signals and signals != "—":
            header_bits.append(signals)
        header = " · ".join(header_bits)
        with st.expander(header):
            subject = row.get("subject") or ""
            body = row.get("body") or ""
            if subject:
                st.markdown(f"**Subject:** {subject}")
            if body:
                st.markdown("**Body:**")
                st.text(body)
            if not subject and not body:
                st.caption("No stored content for this event.")

st.divider()

# ---------- Sent folder ----------
st.subheader("Sent folder")
st.caption("All emails successfully pushed to Instantly. Newest first.")
try:
    sent_df = _sent_emails_cached(workspace_id=_ws_id)
except Exception as exc:
    st.error(f"Could not load sent folder: {exc}")
    sent_df = pd.DataFrame()

if sent_df.empty:
    st.info("Nothing here yet — push an email to Instantly from a lead's detail page.")
else:
    summary_df = pd.DataFrame({
        "Lead": sent_df["lead"],
        "Company": sent_df["company"],
        "Email subject": sent_df["subject"],
        "Sent at": sent_df["sent_at"].map(_format_event_when),
    })
    st.dataframe(summary_df, hide_index=True, width="stretch", key="eng_sent_folder_table")

    option_labels: dict[int, str] = {}
    for _, row in sent_df.iterrows():
        when = _format_event_when(row["sent_at"])
        lead = row["lead"] or "(unknown lead)"
        company = row["company"]
        subject = row["subject"] or "(no subject)"
        bits = [when, lead]
        if company:
            bits.append(company)
        bits.append(subject)
        option_labels[int(row["content_id"])] = " · ".join(bits)

    selected_id = st.selectbox(
        "Select a lead to view email",
        options=list(option_labels.keys()),
        format_func=lambda cid: option_labels[cid],
        index=None,
        placeholder="Choose a sent email…",
        key="eng_sent_folder_select",
    )
    if selected_id is not None:
        row = sent_df.loc[sent_df["content_id"] == selected_id].iloc[0]
        with st.container(border=True):
            if row["subject"]:
                st.markdown(f"**Subject:** {row['subject']}")
            body = row["body"] or ""
            if body:
                st.markdown("**Body:**")
                st.text(body)
            else:
                st.caption("No body stored for this send.")
