"""Dashboard — KPIs, tier distribution, recent pipeline events, usage guide."""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.lib.components import kpi_card
from app.lib.db_queries import (
    kpi_counts,
    list_unrated_content,
    rating_summary_per_content_type,
    tier_distribution,
)
from app.lib.formatters import fmt_duration_ms, fmt_timestamp
from app.lib.log_reader import parse_recent_pipeline_events
from app.styles import inject_styles

inject_styles()


@st.cache_data(ttl=30)
def _kpi_counts_cached() -> dict:
    return kpi_counts()


@st.cache_data(ttl=30)
def _tier_distribution_cached() -> dict:
    return tier_distribution()


@st.cache_data(ttl=30)
def _recent_events_cached(limit: int = 10) -> list[dict]:
    return parse_recent_pipeline_events(limit=limit)


@st.cache_data(ttl=30)
def _review_queue_cached(content_types: tuple[str, ...]) -> pd.DataFrame:
    if not content_types:
        return list_unrated_content()
    frames = [list_unrated_content(content_type=ct) for ct in content_types]
    return pd.concat(frames, ignore_index=True) if frames else list_unrated_content()


@st.cache_data(ttl=30)
def _rating_trends_cached(days: int = 30) -> pd.DataFrame:
    return rating_summary_per_content_type(days=days)


st.title("Dashboard")
st.caption("SDR Enablement Pipeline — at a glance")

# ----- KPIs -----
try:
    kpis = _kpi_counts_cached()
except Exception as exc:
    st.error(f"Could not load KPIs: {exc}")
    kpis = {"leads_total": 0, "enriched": 0, "scored": 0, "sent": 0, "replied": 0}

reply_rate_str = (
    f"{(kpis['replied'] / kpis['sent']) * 100:.1f}%" if kpis["sent"] else "—"
)

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    kpi_card("Total leads", f"{kpis['leads_total']:,}")
with c2:
    kpi_card("Enriched", f"{kpis['enriched']:,}")
with c3:
    kpi_card("Scored", f"{kpis['scored']:,}")
with c4:
    kpi_card("Sent", f"{kpis['sent']:,}")
with c5:
    kpi_card("Reply rate", reply_rate_str, numeric_font="serif")

st.divider()

# ----- Tier distribution + Recent events side by side -----
col_left, col_right = st.columns([1, 2])

with col_left:
    st.subheader("Tier distribution")
    try:
        dist = _tier_distribution_cached()
        if sum(dist.values()) == 0:
            st.info("No scored leads yet. Run the pipeline to see tier breakdown.")
        else:
            df_dist = pd.DataFrame(
                {"tier": list(dist.keys()), "count": list(dist.values())}
            ).set_index("tier")
            st.bar_chart(df_dist, height=240)
    except Exception as exc:
        st.error(f"Could not load tier distribution: {exc}")

with col_right:
    st.subheader("Recent pipeline events")
    try:
        events = _recent_events_cached(limit=10)
        if not events:
            st.info(
                "No pipeline runs logged yet. Use the Run Pipeline page to kick one off."
            )
        else:
            rows = []
            for e in events:
                rows.append(
                    {
                        "When": fmt_timestamp(e.get("ts")),
                        "Lead": e.get("lead_id"),
                        "Tier": e.get("tier") or "—",
                        "OK": "✅" if e.get("ok") else "❌",
                        "Duration": fmt_duration_ms(e.get("duration_ms")),
                    }
                )
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                use_container_width=True,
                key="dashboard_recent_events",
            )
    except Exception as exc:
        st.error(f"Could not parse logs: {exc}")

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
