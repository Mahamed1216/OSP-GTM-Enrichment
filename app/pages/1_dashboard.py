"""Dashboard — KPIs, tier distribution, recent pipeline events, usage guide."""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.lib.components import (
    activity_feed,
    activity_row,
    kpi_card,
    leads_by_tier_chart,
    promo_block,
)
from app.lib.db_queries import (
    kpi_counts,
    list_unrated_content,
    rating_summary_per_content_type,
    ready_to_send_count,
    recent_activity,
    tier_distribution,
)
from app.lib.formatters import fmt_timestamp
from app.styles import inject_styles

inject_styles()


@st.cache_data(ttl=30)
def _kpi_counts_cached() -> dict:
    return kpi_counts()


@st.cache_data(ttl=30)
def _tier_distribution_cached() -> dict:
    return tier_distribution()


@st.cache_data(ttl=30)
def _recent_activity_cached(limit: int = 8) -> list[dict]:
    return recent_activity(limit=limit)


@st.cache_data(ttl=30)
def _ready_to_send_cached() -> int:
    return ready_to_send_count()


@st.cache_data(ttl=30)
def _review_queue_cached(content_types: tuple[str, ...]) -> pd.DataFrame:
    if not content_types:
        return list_unrated_content()
    frames = [list_unrated_content(content_type=ct) for ct in content_types]
    return pd.concat(frames, ignore_index=True) if frames else list_unrated_content()


@st.cache_data(ttl=30)
def _rating_trends_cached(days: int = 30) -> pd.DataFrame:
    return rating_summary_per_content_type(days=days)


st.markdown(
    '<div class="hero-block">'
    '<h1 class="hero-headline">Outbound that lands.</h1>'
    '<p class="hero-sublabel">Lead enrichment, scoring, and personalized outreach '
    'for SDR teams. Every signal cited.</p>'
    '</div>',
    unsafe_allow_html=True,
)

# ----- KPIs -----
try:
    kpis = _kpi_counts_cached()
except Exception as exc:
    st.error(f"Could not load KPIs: {exc}")
    kpis = {
        "leads_total": 0, "enriched": 0, "scored": 0,
        "sent": 0, "replied": 0,
    }

verified_emails = kpis["enriched"]  # enrichment success implies verified email
try:
    ready_n = _ready_to_send_cached()
except Exception:
    ready_n = max(0, kpis.get("scored", 0) - kpis.get("sent", 0))

c1, c2, c3, c4 = st.columns(4)
with c1:
    kpi_card(
        "Leads enriched",
        f"{kpis['enriched']:,}",
        meta=f"of {kpis['leads_total']:,} total",
        icon="ti-target",
        state="primary",
    )
with c2:
    kpi_card(
        "Verified emails",
        f"{verified_emails:,}",
        meta="ready for outreach",
        icon="ti-mail-check",
        state="success",
    )
with c3:
    kpi_card(
        "Ready to send",
        f"{ready_n:,}",
        meta="content drafted, not yet pushed",
        icon="ti-send",
        state="warning",
    )
with c4:
    kpi_card(
        "Already replied",
        f"{kpis.get('replied', 0):,}",
        meta=f"{(kpis['replied'] / kpis['sent']) * 100:.1f}% reply rate"
        if kpis.get("sent") else "no sends yet",
        icon="ti-message-2",
        state="success",
    )

# ----- Two-column: tier chart + activity feed -----
col_left, col_right = st.columns([1, 1])

with col_left:
    try:
        dist = _tier_distribution_cached()
    except Exception as exc:
        st.error(f"Could not load tier distribution: {exc}")
        dist = {}
    if dist:
        st.markdown(leads_by_tier_chart(dist), unsafe_allow_html=True)
    else:
        st.info("No leads yet — ingest a CSV to see tier distribution.")

with col_right:
    try:
        feed_events = _recent_activity_cached(limit=8)
    except Exception as exc:
        st.error(f"Could not load activity: {exc}")
        feed_events = []
    rows_html = [
        activity_row(e["kind"], e["text"], fmt_timestamp(e["when"]))
        for e in feed_events
    ]
    st.markdown(activity_feed(rows_html), unsafe_allow_html=True)

# ----- Cobalt promo / CTA -----
if ready_n > 0:
    promo_block(
        headline=f"Ready to push {ready_n} leads to Instantly.",
        body=(
            "These leads have verified emails and personalized content drafted. "
            "Push the batch in one click — every signal cited."
        ),
        cta_label="Push to Instantly →",
        cta_target_page="pages/2_leads.py",
    )
elif kpis["leads_total"] > kpis["enriched"]:
    waiting = kpis["leads_total"] - kpis["enriched"]
    promo_block(
        headline=f"{waiting} leads waiting for enrichment.",
        body=(
            "Run the pipeline to enrich, score, and generate outreach in one pass. "
            "Sources are cited per lead so SDRs can verify every claim."
        ),
        cta_label="Run pipeline →",
        cta_target_page="pages/4_run_pipeline.py",
    )
elif kpis["leads_total"] == 0:
    promo_block(
        headline="No leads ingested yet.",
        body=(
            "Upload a CSV to ingest your first batch. The pipeline will enrich, "
            "score, and draft personalized outreach in one pass."
        ),
        cta_label="Run pipeline →",
        cta_target_page="pages/4_run_pipeline.py",
    )
else:
    promo_block(
        headline="Pipeline caught up.",
        body=(
            "All current leads are enriched and processed. Sync engagement to see "
            "the latest replies, or open Leads to drill into individual performance."
        ),
        cta_label="View leads →",
        cta_target_page="pages/2_leads.py",
    )

st.divider()

# ----- Review queue -----
st.subheader("Awaiting your review")

kind_filter = st.multiselect(
    "Content type",
    options=["email", "call_script", "linkedin_msg"],
    default=[],
    key="dash_review_filter_kind",
    placeholder="All types",
)

try:
    queue_df = _review_queue_cached(tuple(kind_filter))
except Exception as exc:
    st.error(f"Could not load review queue: {exc}")
    queue_df = pd.DataFrame(
        columns=["id", "lead_id", "Lead", "Company", "Type", "Tier", "Created"]
    )

st.caption(f"{len(queue_df)} item(s) awaiting review")

if queue_df.empty:
    st.info("No content is awaiting review.")
else:
    display_df = queue_df.copy()
    display_df["Created"] = display_df["Created"].apply(fmt_timestamp)
    selection = st.dataframe(
        display_df,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "id": None,        # hide raw content id
            "lead_id": None,   # hide raw lead id
            "Lead": st.column_config.TextColumn("Lead"),
            "Company": st.column_config.TextColumn("Company"),
            "Type": st.column_config.TextColumn("Type"),
            "Tier": st.column_config.TextColumn("Tier"),
            "Created": st.column_config.TextColumn("Created"),
        },
        key="dashboard_review_queue",
    )
    selected = selection.selection.rows if selection and selection.selection else []
    if selected:
        row_idx = selected[0]
        st.session_state["selected_lead_id"] = int(queue_df.iloc[row_idx]["lead_id"])
        st.switch_page("pages/3_lead_detail.py")

st.divider()

# ----- Rating trends -----
st.subheader("Rating trends (last 30 days)")
try:
    trends_df = _rating_trends_cached(days=30)
except Exception as exc:
    st.error(f"Could not load rating trends: {exc}")
    trends_df = pd.DataFrame()

if trends_df.empty or trends_df.shape[0] < 2:
    st.info(
        "Trend chart appears once you have rated content across multiple days."
    )
else:
    st.line_chart(trends_df, height=260)
    st.caption("Daily thumbs-up rate per content type.")

st.divider()

with st.expander("How to use this demo", expanded=False):
    st.markdown(
        """
- **Leads**: browse all leads, filter by tier/status, click a row for full detail.
- **Run Pipeline**: upload a CSV to ingest, then run the pipeline (dry-run by default).
- **Engagement**: sync reply data, view winning examples, and the 30-day reply trend.
        """.strip()
    )
