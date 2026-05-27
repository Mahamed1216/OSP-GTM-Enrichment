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
    sync_campaign_analytics,
    sync_engagement,
)
from src.feedback.learning import process_ratings, promote_winners
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
    SAMPLE_LOW_CONF_MAX,
    approve_recommendation,
    diagnose,
    kpi_view,
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
def _kpi_counts_cached() -> dict[str, int]:
    return kpi_counts()


@st.cache_data(ttl=15)
def _instantly_snapshot_cached() -> dict | None:
    return latest_instantly_snapshot()


@st.cache_data(ttl=15)
def _local_sent_cached() -> int:
    return local_sent_count()


@st.cache_data(ttl=15)
def _perf_by_prompt_cached() -> list[dict]:
    return performance_by_prompt_version()


@st.cache_data(ttl=15)
def _recommendations_cached() -> list[dict]:
    return list_recommendations(limit=10)


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


@st.cache_data(ttl=15)
def _sent_emails_cached() -> "pd.DataFrame":
    return sent_emails()


def _format_event_when(ts) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    try:
        return ts.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


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
_snapshot = _instantly_snapshot_cached()
if _snapshot is not None:
    st.caption(
        f"Last synced from Instantly: {_format_timestamp(_snapshot.get('synced_at'))} "
        f"(weekdays 9 AM ET via GitHub Actions)."
    )
else:
    st.caption("Not yet synced from Instantly — hit the button below or wait for the 9 AM ET job.")

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
    local_before = _local_sent_cached()
    try:
        with st.spinner("Syncing from Instantly…"):
            analytics_result = run_async(sync_campaign_analytics())
            per_lead = run_async(sync_engagement())
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

c1, c2, c3, c4 = st.columns(4)
with c1:
    kpi_card("Sent", f"{sent_remote:,}")
with c2:
    kpi_card(
        "Reply rate",
        _format_pct(replies, sent_remote),
        numeric_font="serif",
    )
with c3:
    kpi_card("Open rate", _format_pct(opens, sent_remote))
with c4:
    kpi_card("Bounce rate", _format_pct(bounces, sent_remote))

if _snapshot is not None and contacted and contacted != sent_remote:
    st.caption(
        f"Instantly sequence started: {contacted:,} · "
        f"emails sent: {sent_remote:,} · "
        f"unique opens: "
        f"{_snapshot.get('unique_open_count') if _snapshot.get('unique_open_count') is not None else '—'} "
        f"· clicks: {int(_snapshot.get('click_count') or 0):,}"
    )

# Sync-hygiene warning: surfaced ONLY here, never as a self-improvement
# recommendation. The loop's delivery branch is informational; this
# warning is the operator-facing version.
_local_sent = _local_sent_cached()
if _snapshot is not None and contacted and abs(contacted - _local_sent) >= 5:
    st.warning(
        f"Sync hygiene: Instantly has {contacted:,} sequence started, but "
        f"local DB has {_local_sent:,} sent records for this campaign. "
        "Investigate the gap — this is a tracking issue, not a copy issue."
    )

with st.expander("Sync debug — raw Instantly analytics + DB comparison"):
    if _snapshot is None:
        st.info("No analytics snapshot yet. Hit \"Sync engagement from Instantly\".")
    else:
        _raw = _snapshot.get("raw") or {}
        # The snapshot's `raw` field holds ONLY the matched record (set by
        # sync_campaign_analytics), so derived ids/names here are the
        # selected campaign — never an account-wide aggregate.
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
        st.markdown("**Local vs Instantly comparison**")
        st.dataframe(
            pd.DataFrame(
                [
                    {"metric": "sent / sequence started", "local DB": _local_sent, "Instantly": contacted},
                    {"metric": "emails sent", "local DB": _local_sent, "Instantly": sent_remote},
                    {"metric": "opens", "local DB": "(per-lead)", "Instantly": opens},
                    {"metric": "replies", "local DB": "(per-lead)", "Instantly": replies},
                    {"metric": "bounces", "local DB": "(per-lead)", "Instantly": bounces},
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.markdown("**Raw Instantly analytics response**")
        st.json(_snapshot.get("raw") or {})

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
            sig = f"{diag.bottleneck}_{diag.loop_status}_{int(diag.current_metric_value * 10000)}"
            pending_rec_key = f"sil_pending_rec_{sig}"
            if pending_rec_key not in st.session_state:
                st.session_state[pending_rec_key] = save_recommendation(diag)
            rec_id = st.session_state[pending_rec_key]

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
                    approve_recommendation(rec_id, approved_by="demo_sdr")
                except Exception as exc:
                    st.error(f"Approval failed: {exc}")
                else:
                    st.cache_data.clear()
                    st.session_state.pop(pending_rec_key, None)
                    st.success(
                        "Approved — addendum appended to the email prompt "
                        "overlay. Future generations will use it; already-"
                        "sent emails are unchanged."
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
    recs = _recommendations_cached()
    if not recs:
        st.caption("No recommendations yet.")
    else:
        for rec in recs:
            status_icon = {
                "approved": "✓",
                "rejected": "✗",
                "draft": "·",
                "ready_for_approval": "⏳",
            }.get(rec["status"], "?")
            header = (
                f"{status_icon} {rec['status'].replace('_', ' ').capitalize()} · "
                f"{rec['bottleneck']} · "
                f"{rec['current_metric_label']} "
                f"{rec['current_metric_value'] * 100:.1f}% → "
                f"target {rec['target_metric_value'] * 100:.1f}% · "
                f"sample {rec['sample_size']} · "
                f"{_format_timestamp(rec['created_at'])}"
            )
            with st.container(border=True):
                st.markdown(header)
                st.caption(rec["diagnosis"])
                if rec.get("proposed_addendum"):
                    with st.expander("Addendum that was proposed"):
                        st.code(rec["proposed_addendum"], language="markdown")
                if rec["approved_by"]:
                    st.caption(
                        f"By {rec['approved_by']} at "
                        f"{_format_timestamp(rec['approved_at'])}"
                    )
                if (
                    rec["status"] == "approved"
                    and rec["previous_prompt_snapshot"]
                    and rec["channel"]
                ):
                    if st.button(
                        "Roll back this change",
                        key=f"sil_rollback_{rec['id']}",
                        type="secondary",
                    ):
                        try:
                            rollback_recommendation(rec["id"])
                        except Exception as exc:
                            st.error(f"Rollback failed: {exc}")
                        else:
                            st.cache_data.clear()
                            st.success("Rolled back to previous prompt overlay.")
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
    sent_df = _sent_emails_cached()
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
