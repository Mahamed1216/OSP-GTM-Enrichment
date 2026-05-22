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
    recent_engagement,
    reply_rate_series,
)
from app.styles import inject_styles

inject_styles()
from src.content.winners import (
    list_all_negatives,
    list_all_winners,
    set_negative_active,
    set_winner_active,
)
from src.feedback.engagement import sync_engagement
from src.feedback.learning import process_ratings, promote_winners

DEMOTE_TTL = 10.0

_KIND_LABELS = {"email": "Email", "call_script": "Call Script", "linkedin_msg": "LinkedIn DM"}
_TYPE_FILTER_OPTIONS = ["All", "email", "call_script", "linkedin_msg"]


@st.cache_data(ttl=15)
def _kpi_counts_cached() -> dict[str, int]:
    return kpi_counts()


@st.cache_data(ttl=15)
def _last_sync_cached() -> datetime | None:
    return last_sync_time()


@st.cache_data(ttl=15)
def _reply_rate_cached(days: int) -> pd.DataFrame:
    return reply_rate_series(days=days)


@st.cache_data(ttl=15)
def _winners_cached() -> list[dict]:
    return list_all_winners()


@st.cache_data(ttl=15)
def _negatives_cached() -> list[dict]:
    return list_all_negatives()


@st.cache_data(ttl=15)
def _recent_engagement_cached(limit: int) -> "pd.DataFrame":
    return recent_engagement(limit=limit)


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


def _library_dataframe(entries: list[dict], type_filter: str) -> pd.DataFrame:
    rows = []
    for e in entries:
        ct = e.get("content_type") or "email"
        if type_filter != "All" and ct != type_filter:
            continue
        body = _entry_body(e)
        snippet = body[:80].replace("\n", " ").strip()
        if len(body) > 80:
            snippet += "…"
        rows.append({
            "id": e.get("id"),
            "Active": "✓" if e.get("is_active", True) else "—",
            "Type": _KIND_LABELS.get(ct, ct),
            "Source": e.get("source") or "—",
            "Score": e.get("score"),
            "Added": (e.get("added_at") or "")[:10],
            "Snippet": snippet,
        })
    return pd.DataFrame.from_records(
        rows, columns=["id", "Active", "Type", "Source", "Score", "Added", "Snippet"]
    )


def _format_pct(num: int, denom: int) -> str:
    if not denom:
        return "—"
    return f"{(num / denom) * 100:.1f}%"


def _format_timestamp(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    return ts.strftime("%Y-%m-%d %H:%M:%S")


st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Engagement.</h1>'
    '<p class="hero-sublabel">Replies, opens, bounces, sends. The loop closing.</p>'
    '</div>',
    unsafe_allow_html=True,
)

# ---------- Sync controls ----------
_sync_file = Path("data/last_engagement_sync.txt")
if _sync_file.exists():
    st.caption(f"Last synced: {_sync_file.read_text().strip()}")
else:
    last_sync = _last_sync_cached()
    if last_sync is not None:
        st.caption(f"Last synced: {_format_timestamp(last_sync)}")
    else:
        st.caption("Not yet synced — sync runs weekdays at 8 AM ET.")

sync_col, promote_col = st.columns(2)
with sync_col:
    sync_clicked = st.button(
        "Sync engagement from Instantly",
        type="primary",
        key="eng_sync_btn",
        help="Pulls fresh delivery/open/reply/bounce data from Instantly into the Engagement table.",
    )
with promote_col:
    promote_clicked = st.button(
        "Promote winners now",
        type="secondary",
        key="eng_promote_btn",
        help=(
            "Promote engagement replies to the winners library and roll up "
            "thumbs-up/down ratings into winners/negatives. Calls "
            "promote_winners() and process_ratings()."
        ),
    )

if sync_clicked:
    try:
        with st.spinner("Syncing from Instantly…"):
            result = run_async(sync_engagement())
    except Exception as exc:
        st.error(f"Sync failed: {exc}")
    else:
        st.cache_data.clear()
        st.success(
            f"Sync complete — {result.get('synced', 0)} updated, "
            f"{result.get('failed', 0)} failed, "
            f"{result.get('total', 0)} total."
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

# ---------- KPI row ----------
try:
    kpis = _kpi_counts_cached()
except Exception as exc:
    st.error(f"Could not load KPIs: {exc}")
    kpis = {"sent": 0, "replied": 0, "opened": 0, "bounced": 0}

c1, c2, c3, c4 = st.columns(4)
with c1:
    kpi_card("Sent", f"{kpis.get('sent', 0):,}")
with c2:
    kpi_card("Reply rate", _format_pct(kpis.get("replied", 0), kpis.get("sent", 0)), numeric_font="serif")
with c3:
    kpi_card("Open rate", _format_pct(kpis.get("opened", 0), kpis.get("sent", 0)))
with c4:
    kpi_card("Bounce rate", _format_pct(kpis.get("bounced", 0), kpis.get("sent", 0)))

st.divider()

# ---------- Reply-rate chart ----------
st.subheader("Reply rate over time")
days = st.number_input(
    "Days", min_value=7, max_value=90, value=30, step=1, key="eng_days",
    help="Window for the reply-rate trend below.",
)
try:
    rr_df = _reply_rate_cached(int(days))
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
) -> None:
    st.subheader(title)
    filter_value = st.selectbox(
        "Content type",
        options=_TYPE_FILTER_OPTIONS,
        index=0,
        key=f"{key_prefix}_type_filter",
    )
    df = _library_dataframe(entries, filter_value)
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


_render_library(
    "Winners library",
    _winners_cached(),
    empty_msg="No winners yet. Hit \"Promote winners now\" once engagement replies or thumbs-up ratings exist.",
    key_prefix="eng_winners",
    set_active=set_winner_active,
    item_noun="winner",
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
    recent_df = _recent_engagement_cached(int(recent_limit))
except Exception as exc:
    st.error(f"Could not load recent engagement: {exc}")
    recent_df = pd.DataFrame()

if recent_df.empty:
    st.info(
        "Engagement events appear here after the first sync from Instantly "
        "(provided you have an INSTANTLY_API_KEY set and at least one email delivered)."
    )
else:
    pretty = recent_df.copy()
    for col in ("sent", "delivered", "opened", "clicked", "replied"):
        pretty[col] = pretty[col].map(lambda v: "✓" if v else "—")
    pretty["bounced"] = pretty["bounced"].map(lambda v: "❌" if v else "—")
    pretty["kind"] = pretty["kind"].map(lambda k: _KIND_LABELS.get(k, k))
    pretty = pretty.rename(columns={
        "synced_at": "When", "lead": "Lead", "company": "Company", "kind": "Type",
        "sent": "Sent", "delivered": "Deliv", "opened": "Open", "clicked": "Click",
        "replied": "Reply", "bounced": "Bounce",
    })
    st.dataframe(pretty, hide_index=True, width="stretch", key="eng_recent_events_table")
