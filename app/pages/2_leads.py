"""Leads — filterable, sortable table; select rows for bulk actions or open detail."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.lib.async_runner import run_async
from app.lib.badges import status_pill, tier_badge
from app.lib.db_queries import list_leads
from app.styles import inject_styles
from src.config import settings
from src.db import session_scope
from src.delivery.eligibility import SKIP_LABELS, filter_eligible, summarize_skips
from src.delivery.instantly import deliver_email, get_campaign
from src.db import session_scope as _delete_session_scope
from src.leads import delete_lead, reset_lead_sequence

inject_styles()

_CONFIRM_TTL = 5.0  # seconds for two-click confirm window
_BULK_PUSH_HARD_LIMIT = 10  # over this requires "I understand" checkbox

@st.cache_data(ttl=30)
def _list_leads_cached() -> pd.DataFrame:
    return list_leads()


def _row_status(row: pd.Series) -> str:
    """Pick the dominant delivery state for the Status pill.

    Replied dominates Sent (any reply implies a send); Sent dominates the
    default Pending. Bounced is surfaced when the engagement flag is set
    on the existing list_leads frame — currently we don't carry bounced
    into the leads frame, so this collapses to {replied, sent, pending}.
    """
    if bool(row.get("Replied")):
        return status_pill("replied")
    if bool(row.get("Sent")):
        return status_pill("sent")
    return status_pill("pending")


st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Leads.</h1>'
    '<p class="hero-sublabel">Every prospect, scored. Ready to push.</p>'
    '</div>',
    unsafe_allow_html=True,
)

try:
    df = _list_leads_cached()
except Exception as exc:
    st.error(f"Could not load leads: {exc}")
    st.stop()

if df.empty:
    st.info("No leads yet. Use the Run Pipeline page to ingest a CSV.")
    st.stop()

# ---------- Filters ----------
# Wrapper is a presentational marker — Streamlit places its native widgets
# in their own DOM tree, so this div mostly serves as visual scaffolding and
# may not wrap the widgets at the DOM level. Kept for parity with the rest
# of the design system (see .filter-row in app/styles.py).
st.markdown('<div class="filter-row">', unsafe_allow_html=True)
fc1, fc2, fc3, _ = st.columns([2, 1, 1, 3])
with fc1:
    tier_filter = st.multiselect(
        "Tier",
        options=["A", "B", "C"],
        default=[],
        key="leads_filter_tier",
        placeholder="All tiers",
    )
with fc2:
    sent_only = st.checkbox("Sent only", key="leads_filter_sent")
with fc3:
    enriched_only = st.checkbox("Has enrichment", key="leads_filter_enriched")
st.markdown('</div>', unsafe_allow_html=True)

filtered = df.copy()
if tier_filter:
    filtered = filtered[filtered["Tier"].isin(tier_filter)]
if sent_only:
    filtered = filtered[filtered["Sent"]]
if enriched_only:
    filtered = filtered[filtered["Enriched"]]

# Markdown-rendered Tier + Status columns for display; keep the original Tier
# (single-letter) for filtering above. Streamlit's :color-background[] shorthand
# is rendered natively in dataframe string cells when no explicit column type
# overrides it — we leave Tier and Status without an explicit column_config so
# they auto-render as pills.
display_df = filtered.copy()
display_df["Tier"] = display_df["Tier"].apply(tier_badge)
display_df["Status"] = filtered.apply(_row_status, axis=1)

if filtered.empty:
    st.info("No leads match the current filters.")
    st.stop()

st.caption(f"{len(filtered)} of {len(df)} leads")

selection = st.dataframe(
    display_df,
    hide_index=True,
    use_container_width=True,
    on_select="rerun",
    selection_mode="multi-row",
    column_order=["id", "Name", "Company", "Title", "Tier", "Score", "Enriched", "Status"],
    column_config={
        "id": st.column_config.NumberColumn("ID", width="small"),
        "Score": st.column_config.NumberColumn("Score", format="%d"),
        "Enriched": st.column_config.CheckboxColumn("Enriched"),
        "Sent": None,       # hidden — Status column subsumes
        "Replied": None,    # hidden — Status column subsumes
        # Tier + Status: no explicit type → Streamlit renders the
        # :color-background[] markdown shorthand as colored pills.
    },
    key="leads_table",
)

selected_rows = selection.selection.rows if selection and selection.selection else []
selected_lead_ids = [int(filtered.iloc[i]["id"]) for i in selected_rows if i < len(filtered)]

# ---------- Action bar (visible only when ≥1 row selected) ----------
if selected_lead_ids:
    n = len(selected_lead_ids)
    st.divider()
    with st.container(border=True):
        ac1, ac2, ac3, ac4 = st.columns([2, 1.4, 1.6, 1.2])
        ac1.markdown(f"**{n} selected**")

        # Open (single-row only) — replaces old auto-switch on row click.
        if n == 1:
            if ac2.button("Open lead", key="open_selected"):
                st.session_state["selected_lead_id"] = selected_lead_ids[0]
                st.switch_page("pages/3_lead_detail.py")
        else:
            ac2.empty()

        # Delete selected — two-click confirm
        _del_key = "delete_selected_pending"
        pending_at = st.session_state.get(_del_key)
        confirming = bool(pending_at) and (time.time() - pending_at) < _CONFIRM_TTL

        if confirming:
            if ac3.button(
                f"Confirm delete {n}",
                type="primary",
                key="confirm_delete_selected",
            ):
                st.session_state.pop(_del_key, None)
                status = st.status(f"Deleting {n} leads…", expanded=True)
                deleted = 0
                with status:
                    for lid in selected_lead_ids:
                        result = delete_lead(lid)
                        if result.get("success"):
                            deleted += 1
                            st.write(f"✓ deleted lead {lid}")
                        else:
                            st.write(f"⚠️ lead {lid}: {result.get('reason', 'unknown')}")
                # If the table is now empty, snap the id sequence back to 1
                # so the next ingest starts fresh. No-op otherwise.
                try:
                    with _delete_session_scope() as _sess:
                        reset_lead_sequence(_sess)
                except Exception as _exc:
                    # Sequence reset is best-effort cosmetics — don't fail
                    # the delete UX if the DDL is denied.
                    st.write(f"⚠ Could not reset id sequence: {_exc}")
                status.update(label=f"Deleted {deleted} of {n} leads.", state="complete")
                st.toast(f"Deleted {deleted} leads.")
                st.cache_data.clear()
                # Clear widget selection so deleted rows aren't "remembered"
                st.session_state.pop("leads_table", None)
                st.rerun()
        else:
            if ac3.button(
                "Delete selected",
                key="delete_selected",
                help="Permanently removes leads + all dependent rows.",
            ):
                st.session_state[_del_key] = time.time()
                st.rerun()

        if ac4.button("Clear selection", key="clear_selection"):
            st.session_state.pop("leads_table", None)
            st.session_state.pop(_del_key, None)
            st.session_state.pop("push_selected_pending", None)
            st.rerun()

        if confirming:
            st.caption(":red[Click \"Confirm delete\" within 5s to proceed.]")

    # ---------- 9d: bulk push to Instantly ----------
    with session_scope() as _s:
        eligible_ids, skipped = filter_eligible(selected_lead_ids, _s)
    n_eligible = len(eligible_ids)
    skip_summary = summarize_skips(skipped)

    with st.container(border=True):
        push_label = f"Push to Instantly ({n_eligible} eligible)"
        pc1, pc2 = st.columns([1.5, 5])
        push_clicked = pc1.button(
            push_label,
            key="push_to_instantly",
            disabled=(n_eligible == 0),
            type="secondary",
        )
        if n_eligible:
            pc2.caption(
                f"{n_eligible} of {n} selected eligible. "
                + (f"Skipped: {skip_summary}." if skip_summary != "none" else "")
            )
        else:
            # Promote the cause: when 0 eligible, the user can't act and
            # needs to know WHY at a glance, with a concrete remediation hint.
            dominant_code, dominant_ids = max(
                skipped.items(), key=lambda kv: len(kv[1]), default=(None, [])
            )
            _HINTS = {
                "below_tier": (
                    f"To push lower-tier leads, lower `SEND_MIN_TIER` in `.env` "
                    f"(currently `{settings.send_min_tier}`)."
                ),
                "no_content": "Regenerate email content from Lead Detail → Generated Content.",
                "missing_email": "These leads have no email address — re-ingest from a source that includes it.",
                "email_unverified": "Run the verifier on these leads first, or set their `email_verification_status` to `Verified`.",
                "already_sent": "These leads have already been delivered — pick different leads or reset their delivery state from Lead Detail.",
                "in_progress": "These leads are mid-send; wait for the in-progress attempt to finish or fail.",
            }
            hint = _HINTS.get(dominant_code or "", "")
            dominant_label = SKIP_LABELS.get(dominant_code or "", "unknown reason")
            pc2.info(
                f"**No leads eligible.** {len(dominant_ids)} of {n} blocked by "
                f"**{dominant_label}**. {hint}\n\n"
                f"Full breakdown: {skip_summary}."
            )

        _push_key = "push_selected_pending"
        if push_clicked and n_eligible:
            st.session_state[_push_key] = time.time()
            st.rerun()

        push_pending_at = st.session_state.get(_push_key)
        confirming_push = (
            bool(push_pending_at) and (time.time() - push_pending_at) < _CONFIRM_TTL
        )

        if confirming_push and n_eligible:
            # Lazy-fetch campaign info; cache 5min in session_state.
            campaign_cache = st.session_state.get("push_campaign_cache") or {}
            cached_at = campaign_cache.get("at", 0)
            if time.time() - cached_at > 300:
                try:
                    campaign = run_async(get_campaign(settings.instantly_campaign_id))
                    campaign_cache = {"at": time.time(), "data": campaign}
                    st.session_state["push_campaign_cache"] = campaign_cache
                except Exception as exc:
                    st.error(f"Could not fetch Instantly campaign info: {exc}")
                    st.session_state.pop(_push_key, None)
                    st.stop()
            campaign = campaign_cache.get("data") or {}

            name = campaign.get("name") or campaign.get("campaign_name") or "(unnamed)"
            senders = (
                campaign.get("email_list")
                or campaign.get("sending_accounts")
                or ["(unknown)"]
            )
            if isinstance(senders, str):
                senders = [senders]
            daily_limit = (
                campaign.get("daily_limit")
                or campaign.get("rate_limit")
                or "see campaign settings"
            )

            st.markdown("---")
            st.markdown(f"**Push {n_eligible} leads to campaign `{name}`?**")
            st.caption(f"Sender(s): {', '.join(senders)}")
            st.caption(f"Daily send rate: {daily_limit}")

            big_batch_ok = True
            if n_eligible > _BULK_PUSH_HARD_LIMIT:
                big_batch_ok = st.checkbox(
                    f"I understand this will send {n_eligible} real emails.",
                    key="bulk_push_big_ack",
                )

            cc1, cc2 = st.columns([1.5, 5])
            if cc1.button(
                f"Confirm push ({n_eligible})",
                type="primary",
                key="confirm_push",
                disabled=not big_batch_ok,
            ):
                st.session_state.pop(_push_key, None)
                progress = st.progress(0.0, text=f"Pushing {n_eligible} lead(s)…")
                status = st.status("Sending…", expanded=True)
                n_sent = n_failed = 0
                with status:
                    for i, lid in enumerate(eligible_ids, start=1):
                        try:
                            result = run_async(deliver_email(lid, dry_run=False))
                        except Exception as exc:
                            n_failed += 1
                            st.write(f"❌ lead {lid} — {type(exc).__name__}: {exc}")
                        else:
                            if result.delivered:
                                n_sent += 1
                                st.write(f"✅ lead {lid} — sent (id={result.delivery_id})")
                            else:
                                n_failed += 1
                                st.write(
                                    f"⚠️ lead {lid} — skipped: {result.skip_reason}"
                                )
                        progress.progress(
                            i / n_eligible,
                            text=f"Pushed {i} of {n_eligible}…",
                        )
                status.update(
                    label=f"Bulk push complete — {n_sent} sent, {n_failed} failed.",
                    state="complete",
                )
                st.toast(f"{n_sent} sent, {n_failed} failed.")
                st.cache_data.clear()
                st.session_state.pop("leads_table", None)
                st.rerun()
            cc2.caption(":red[Click \"Confirm push\" within 5s to proceed.]")
