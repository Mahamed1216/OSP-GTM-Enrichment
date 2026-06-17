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
from app.lib.db_queries import count_leads, list_leads
from app.lib.workspace_state import get_current_workspace_id, render_workspace_banner
from app.styles import inject_styles
from src.config import settings
from src.db import session_scope
from src.delivery.eligibility import SKIP_LABELS, filter_eligible, summarize_skips
from src.delivery.instantly import (
    debug_overwrite_one_lead,
    deliver_email,
    get_campaign,
    overwrite_lead_copy,
)
from src.db import session_scope as _delete_session_scope
from src.leads import delete_lead, reset_lead_sequence

inject_styles()

_CONFIRM_TTL = 5.0  # seconds for two-click confirm window
_BULK_PUSH_HARD_LIMIT = 10  # over this requires "I understand" checkbox
_PAGE_SIZES = [25, 50, 100, 250]
_DEFAULT_PAGE_SIZE = 50


def _row_status(row: pd.Series) -> str:
    """Pick the dominant delivery state for the Status pill."""
    if bool(row.get("Replied")):
        return status_pill("replied")
    if bool(row.get("Sent")):
        return status_pill("sent")
    return status_pill("pending")


@st.cache_data(ttl=30)
def _count_leads_cached(
    workspace_id: int | None,
    search: str,
    tier_filter: tuple,
    sent_only: bool,
    not_sent_only: bool,
    enriched_only: bool,
    hiring_filter: str = "any",
) -> int:
    return count_leads(
        workspace_id,
        search=search,
        tier_filter=list(tier_filter) if tier_filter else None,
        sent_only=sent_only,
        not_sent_only=not_sent_only,
        enriched_only=enriched_only,
        hiring_filter=hiring_filter,
    )


@st.cache_data(ttl=30)
def _list_leads_cached(
    workspace_id: int | None,
    page: int,
    page_size: int,
    search: str,
    tier_filter: tuple,
    sent_only: bool,
    not_sent_only: bool,
    enriched_only: bool,
    hiring_filter: str = "any",
) -> pd.DataFrame:
    return list_leads(
        workspace_id,
        limit=page_size,
        offset=(page - 1) * page_size,
        search=search,
        tier_filter=list(tier_filter) if tier_filter else None,
        sent_only=sent_only,
        not_sent_only=not_sent_only,
        enriched_only=enriched_only,
        hiring_filter=hiring_filter,
    )


# ---------- Page / session state ----------
_ws_id = get_current_workspace_id()

if "leads_page" not in st.session_state:
    st.session_state["leads_page"] = 1
if "leads_page_size" not in st.session_state:
    st.session_state["leads_page_size"] = _DEFAULT_PAGE_SIZE


def _reset_page() -> None:
    """Reset to page 1 and clear the row selection when any filter changes."""
    st.session_state["leads_page"] = 1
    st.session_state.pop("leads_table", None)


st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Leads.</h1>'
    '<p class="hero-sublabel">Every prospect, scored. Ready to push.</p>'
    '</div>',
    unsafe_allow_html=True,
)

render_workspace_banner()

# ---------- Search ----------
search_query = st.text_input(
    "Search leads",
    placeholder="Search by name or email",
    key="leads_search",
    on_change=_reset_page,
)

# ---------- Filters ----------
def _on_sent_change() -> None:
    if st.session_state.get("leads_filter_sent"):
        st.session_state["leads_filter_not_sent"] = False
    _reset_page()


def _on_not_sent_change() -> None:
    if st.session_state.get("leads_filter_not_sent"):
        st.session_state["leads_filter_sent"] = False
    _reset_page()


st.markdown('<div class="filter-row">', unsafe_allow_html=True)
fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1, 1, 1, 2])
with fc1:
    tier_filter = st.multiselect(
        "Tier",
        options=["A", "B", "C"],
        default=[],
        key="leads_filter_tier",
        placeholder="All tiers",
        on_change=_reset_page,
    )
with fc2:
    sent_only = st.checkbox(
        "Sent only", key="leads_filter_sent", on_change=_on_sent_change
    )
with fc3:
    not_sent_only = st.checkbox(
        "Not sent", key="leads_filter_not_sent", on_change=_on_not_sent_change
    )
with fc4:
    enriched_only = st.checkbox("Has enrichment", key="leads_filter_enriched",
                                on_change=_reset_page)
with fc5:
    hiring_filter = st.selectbox(
        "Hiring signal",
        options=["any", "high", "medium", "none"],
        index=0,
        key="leads_filter_hiring",
        on_change=_reset_page,
    )
st.markdown('</div>', unsafe_allow_html=True)

# ---------- C-tier hiring rescue ----------
# Workspace-scoped: researches hiring signals for C-tier leads missing one
# (limit 25 by default) and applies tier uplifts. Never sends or pushes.
with st.expander("🚀 Run C-tier hiring rescue", expanded=False):
    st.caption(
        "Researches open RevOps / SDR / GTM / automation roles for C-tier "
        "leads in this workspace that have no hiring signal yet, then applies "
        "any tier uplift. Uses Tavily. No emails sent, no Instantly push."
    )
    rc1, rc2 = st.columns([1, 3])
    with rc1:
        _rescue_limit = st.number_input(
            "Limit", min_value=1, max_value=200, value=25, step=5,
            key="leads_rescue_limit",
        )
    if st.button("Run hiring rescue", key="leads_rescue_run", type="primary"):
        try:
            from src.signals.hiring_rescue import run_hiring_rescue_sync
            with st.spinner("Researching hiring signals…"):
                report = run_hiring_rescue_sync(_ws_id, limit=int(_rescue_limit))
            st.success(
                f"Processed {report.processed_count} leads · "
                f"high {report.high_signal_count} / med {report.medium_signal_count} "
                f"/ low {report.low_signal_count} / none {report.no_signal_count} · "
                f"bumped → B: {report.bumped_to_B_count}, → A: {report.bumped_to_A_count}"
                + (f" · {report.errors} errors" if report.errors else "")
            )
            st.cache_data.clear()
        except Exception as exc:
            st.error(f"Hiring rescue failed: {type(exc).__name__}: {exc}")

# ---------- Resolve current pagination state ----------
_page = st.session_state["leads_page"]
_page_size = st.session_state["leads_page_size"]
_search = (search_query or "").strip()
_tiers = tuple(tier_filter)
_sent = bool(st.session_state.get("leads_filter_sent"))
_not_sent = bool(st.session_state.get("leads_filter_not_sent"))
_enriched = bool(st.session_state.get("leads_filter_enriched"))
_hiring = st.session_state.get("leads_filter_hiring", "any")

# ---------- Load data (server-side filtered + paginated) ----------
try:
    _total = _count_leads_cached(_ws_id, _search, _tiers, _sent, _not_sent, _enriched, _hiring)
    df = _list_leads_cached(_ws_id, _page, _page_size, _search, _tiers, _sent, _not_sent, _enriched, _hiring)
except Exception as exc:
    st.error(f"Could not load leads: {exc}")
    st.stop()

if _total == 0:
    if _search or _tiers or _sent or _not_sent or _enriched or _hiring != "any":
        st.info("No leads match the current filters.")
    else:
        st.info("No leads yet in this workspace. Use the Run Pipeline page to ingest a CSV.")
    st.stop()

# Clamp page if it's beyond the last page (e.g. after bulk delete)
_total_pages = max(1, (_total + _page_size - 1) // _page_size)
if _page > _total_pages:
    st.session_state["leads_page"] = _total_pages
    _page = _total_pages

_row_start = (_page - 1) * _page_size + 1
_row_end = min(_page * _page_size, _total)


# ---------- Pagination helper ----------
def _pagination_controls(key_prefix: str) -> None:
    """Render prev / next / info / page-size controls. key_prefix keeps keys unique."""
    pc1, pc2, pc3, pc4 = st.columns([1, 1, 4, 2])
    with pc1:
        if st.button("← Prev", key=f"{key_prefix}_prev", disabled=(_page <= 1)):
            st.session_state["leads_page"] = _page - 1
            st.session_state.pop("leads_table", None)
            st.rerun()
    with pc2:
        if st.button("Next →", key=f"{key_prefix}_next", disabled=(_page >= _total_pages)):
            st.session_state["leads_page"] = _page + 1
            st.session_state.pop("leads_table", None)
            st.rerun()
    with pc3:
        st.caption(
            f"Showing **{_row_start}–{_row_end}** of **{_total}** leads  ·  "
            f"Page {_page} of {_total_pages}"
        )
    with pc4:
        _sz_idx = _PAGE_SIZES.index(_page_size) if _page_size in _PAGE_SIZES else 1
        _new_sz = st.selectbox(
            "Rows per page",
            options=_PAGE_SIZES,
            index=_sz_idx,
            key=f"{key_prefix}_page_size",
            label_visibility="collapsed",
        )
        if _new_sz != _page_size:
            st.session_state["leads_page_size"] = _new_sz
            st.session_state["leads_page"] = 1
            st.session_state.pop("leads_table", None)
            st.rerun()


# ---------- Top pagination ----------
_pagination_controls("pag_top")

# ---------- Bulk select range (operates on the current page) ----------
nc1, nc2, nc3, _ = st.columns([1, 1, 1, 3], vertical_alignment="bottom")
_n_page = len(df)
with nc1:
    select_from = st.number_input(
        "From",
        min_value=1,
        max_value=max(1, _n_page),
        value=min(1, _n_page),
        step=1,
        key="leads_select_from",
    )
with nc2:
    select_to = st.number_input(
        "To",
        min_value=1,
        max_value=max(1, _n_page),
        value=min(_n_page, _n_page),
        step=1,
        key="leads_select_to",
    )
with nc3:
    if st.button("Select", key="leads_select_range_btn"):
        lo = max(1, int(min(select_from, select_to)))
        hi = min(_n_page, int(max(select_from, select_to)))
        st.session_state["leads_table"] = {
            "selection": {"rows": list(range(lo - 1, hi)), "columns": []}
        }
        st.rerun()

# ---------- Build display frame (badge columns only — SQL already filtered) ----------
display_df = df.copy()
display_df["Tier"] = display_df["Tier"].apply(tier_badge)
display_df["Status"] = df.apply(_row_status, axis=1)

selection = st.dataframe(
    display_df,
    hide_index=True,
    use_container_width=True,
    on_select="rerun",
    selection_mode="multi-row",
    column_order=["id", "Name", "Company", "Title", "Tier", "Score", "Enriched", "Hiring", "Src signal", "Status"],
    column_config={
        "id": st.column_config.NumberColumn("ID", width="small"),
        "Score": st.column_config.NumberColumn("Score", format="%d"),
        "Enriched": st.column_config.CheckboxColumn("Enriched"),
        "Hiring": st.column_config.TextColumn("Hiring", width="small"),
        "Src signal": st.column_config.TextColumn("Src signal", width="small"),
        "Sent": None,
        "Replied": None,
        "Email": None,
    },
    key="leads_table",
)

# ---------- Bottom pagination ----------
_pagination_controls("pag_bot")

selected_rows = selection.selection.rows if selection and selection.selection else []
selected_lead_ids = [int(df.iloc[i]["id"]) for i in selected_rows if i < len(df)]

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
                            result = run_async(
                                deliver_email(lid, dry_run=False, workspace_id=_ws_id)
                            )
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

    # ---------- Overwrite existing Instantly copy ----------
    # For when bad copy was already pushed and the local content has been
    # regenerated. Updates `custom_variables` on the matched Instantly lead
    # instead of creating a duplicate; never deletes, never activates the
    # campaign. See src/delivery/instantly.py:overwrite_lead_copy.
    n_selected = len(selected_lead_ids)
    with st.container(border=True):
        st.markdown("**Overwrite existing Instantly copy**")
        st.caption(
            "Updates `personalized_subject` / `personalized_body` on leads "
            "already in the Instantly campaign with the latest generated copy "
            "from this DB. Matches by email + campaign id. No duplicates created; "
            "no campaign activation."
        )

        ack = st.checkbox(
            "Overwrite existing Instantly copy with latest generated copy",
            key="overwrite_ack",
            value=False,
            help=(
                "Required. Confirms you've reviewed the regenerated email copy "
                "and want it pushed to Instantly for the selected leads."
            ),
        )

        create_missing = st.checkbox(
            "Also create leads that aren't in Instantly yet",
            key="overwrite_create_missing",
            value=True,
            help=(
                "When a selected lead has no match in Instantly, push it as a "
                "new lead. Uncheck to update-only (skip unknown leads)."
            ),
        )

        oc1, oc2 = st.columns([1.5, 5])
        overwrite_clicked = oc1.button(
            f"Overwrite for selected ({n_selected})",
            key="overwrite_btn",
            disabled=(n_selected == 0 or not ack),
            type="secondary",
        )
        if n_selected == 0:
            oc2.caption("Select at least one lead to enable overwrite.")
        elif not ack:
            oc2.caption("Check the confirmation box above to enable overwrite.")
        else:
            oc2.caption(f"Ready to overwrite {n_selected} selected lead(s).")

        if overwrite_clicked and n_selected and ack:
            progress = st.progress(0.0, text=f"Overwriting {n_selected} lead(s)…")
            status = st.status("Overwriting…", expanded=True)
            counts = {
                "updated": 0,
                "created": 0,
                "skipped": 0,
                "failed": 0,
                "duplicate_warning": 0,
                "update_unsupported": 0,
            }
            results: list[tuple[int, str, str]] = []
            with status:
                for i, lid in enumerate(selected_lead_ids, start=1):
                    try:
                        res = run_async(
                            overwrite_lead_copy(
                                lid,
                                create_if_missing=create_missing,
                                workspace_id=_ws_id,
                            )
                        )
                    except Exception as exc:
                        counts["failed"] += 1
                        detail = f"{type(exc).__name__}: {exc}"
                        st.write(f"❌ lead {lid} — {detail}")
                        results.append((lid, "failed", detail))
                    else:
                        counts[res.status] = counts.get(res.status, 0) + 1
                        icon = {
                            "updated": "✏️",
                            "created": "➕",
                            "skipped": "⚠️",
                            "failed": "❌",
                            "duplicate_warning": "⚠️",
                            "update_unsupported": "🛑",
                        }.get(res.status, "•")
                        st.write(
                            f"{icon} lead {lid} — **{res.status}** — {res.detail or ''}"
                        )
                        results.append((lid, res.status, res.detail or ""))
                    progress.progress(
                        i / n_selected,
                        text=f"Processed {i} of {n_selected}…",
                    )
            status.update(
                label=(
                    f"Done — {counts['updated']} updated, "
                    f"{counts['created']} created, "
                    f"{counts['skipped']} skipped, "
                    f"{counts['failed']} failed, "
                    f"{counts['duplicate_warning']} duplicate warnings."
                ),
                state="complete",
            )

            # Final summary block — five mandatory counters from the spec
            # plus the update_unsupported bucket (only shown if hit, with a
            # clear next-step for the operator).
            st.markdown("---")
            st.markdown("### Overwrite summary")
            sc1, sc2, sc3, sc4, sc5 = st.columns(5)
            sc1.metric("Updated",        counts["updated"])
            sc2.metric("Created",        counts["created"])
            sc3.metric("Skipped",        counts["skipped"])
            sc4.metric("Failed",         counts["failed"])
            sc5.metric("Duplicate warn", counts["duplicate_warning"])

            if counts["update_unsupported"]:
                offenders = [
                    (lid, detail)
                    for (lid, st_, detail) in results
                    if st_ == "update_unsupported"
                ]
                st.warning(
                    f"**Instantly refused to update {counts['update_unsupported']} "
                    f"lead(s).** These cannot be patched in place — you must "
                    f"delete them from the Instantly campaign and re-import, "
                    f"or use a delete-and-recreate flow."
                )
                with st.expander("Affected leads"):
                    for lid, detail in offenders:
                        st.write(f"- lead {lid} — {detail}")

            if counts["duplicate_warning"]:
                dupes = [
                    (lid, detail)
                    for (lid, st_, detail) in results
                    if st_ == "duplicate_warning"
                ]
                st.warning(
                    f"**{counts['duplicate_warning']} lead(s) have duplicates in "
                    f"Instantly.** Refused to update to avoid touching the wrong "
                    f"record. De-dupe in Instantly first."
                )
                with st.expander("Affected leads"):
                    for lid, detail in dupes:
                        st.write(f"- lead {lid} — {detail}")

            st.session_state.pop("overwrite_ack", None)

    # ---------- Debug Instantly overwrite for selected lead ----------
    # Temporary diagnostic. Runs ONE search + ONE PATCH + ONE GET on a
    # single lead, no retries, and dumps every URL / body / status / JSON
    # so we can prove which of A-E is true (wrong id / wrong endpoint /
    # wrong body / wrong key / unsupported). Does not delete, does not
    # activate the campaign, does not touch multiple leads.
    with st.container(border=True):
        st.markdown("**Debug Instantly overwrite for selected lead**")
        debug_enabled = (n_selected == 1)
        if not debug_enabled:
            st.caption(
                f"Select exactly one lead to enable (currently selected: {n_selected})."
            )
        debug_clicked = st.button(
            "Debug Instantly overwrite for selected lead",
            key="debug_overwrite_btn",
            disabled=not debug_enabled,
            type="secondary",
            help=(
                "Single lead. Captures the real Instantly API request URL, "
                "request body, response status, and response JSON for the "
                "search + PATCH + read-back round trip. No retries — failures "
                "surface verbatim."
            ),
        )
        if debug_clicked and debug_enabled:
            target_lid = int(selected_lead_ids[0])
            with st.spinner(f"Running diagnostic on lead {target_lid}…"):
                try:
                    debug = run_async(
                        debug_overwrite_one_lead(target_lid, workspace_id=_ws_id)
                    )
                except Exception as exc:
                    st.error(f"Diagnostic crashed: {type(exc).__name__}: {exc}")
                    debug = None
            if debug:
                # 1-5: local-side facts
                st.markdown("### 1–5 · Local state")
                st.write(f"1. **Local lead id:** `{debug['local_lead_id']}`")
                st.write(f"2. **Local email:** `{debug['local_email']}`")
                st.write(
                    f"3. **Local latest email subject:** "
                    f"`{debug['local_latest_subject']}`"
                )
                st.write("4. **Local latest email body preview (first 280 chars):**")
                st.code(debug["local_latest_body_preview"] or "")
                st.write(f"5. **Campaign id:** `{debug['campaign_id']}`")

                # 6-9: search round-trip
                st.markdown("### 6–9 · Instantly search round-trip")
                search = debug.get("search") or {}
                st.write(f"6. **Endpoint:** `POST {search.get('request_url')}`")
                st.write("7. **Request body:**")
                st.json(search.get("request_body"))
                st.write(
                    f"8. **Response status:** `{search.get('status')}`"
                    + (f" — error: `{search['error']}`" if search.get("error") else "")
                )
                with st.expander("9. Full search response JSON"):
                    if search.get("response_json") is not None:
                        st.json(search["response_json"])
                    else:
                        st.code(search.get("response_text") or "(no body)")
                st.caption(
                    f"Matches with exact email: **{debug.get('search_match_count', 0)}**"
                )

                # 10: which lead we picked
                st.markdown("### 10 · Selected remote lead")
                st.write(
                    f"10. **Instantly lead id:** "
                    f"`{debug.get('selected_remote_lead_id') or '(none)'}`"
                )
                snapshot = debug.get("selected_remote_lead_snapshot")
                if snapshot:
                    with st.expander("Snapshot from search response"):
                        st.json(snapshot)

                # 11-14: PATCH round-trip
                st.markdown("### 11–14 · Instantly update (PATCH) round-trip")
                update = debug.get("update") or {}
                st.write(f"11. **Endpoint:** `PATCH {update.get('request_url')}`")
                st.write("12. **Request body:**")
                st.json(update.get("request_body"))
                st.write(
                    f"13. **Response status:** `{update.get('status')}`"
                    + (f" — error: `{update['error']}`" if update.get("error") else "")
                )
                with st.expander("14. Full update response JSON"):
                    if update.get("response_json") is not None:
                        st.json(update["response_json"])
                    else:
                        st.code(update.get("response_text") or "(no body)")

                # 15-17: read-back round-trip
                st.markdown("### 15–17 · Read-back (GET) round-trip")
                readback = debug.get("readback") or {}
                st.write(f"15. **Endpoint:** `GET {readback.get('request_url')}`")
                st.write(
                    f"16. **Response status:** `{readback.get('status')}`"
                    + (f" — error: `{readback['error']}`" if readback.get("error") else "")
                )
                with st.expander("17. Full read-back response JSON"):
                    if readback.get("response_json") is not None:
                        st.json(readback["response_json"])
                    else:
                        st.code(readback.get("response_text") or "(no body)")

                # 18-20: comparison
                st.markdown("### 18–20 · Comparison")
                st.write(
                    f"18. **Remote `personalized_subject` after PATCH:** "
                    f"`{debug.get('remote_subject_after')}`"
                )
                body_after = debug.get("remote_body_after") or ""
                st.write("19. **Remote `personalized_body` after PATCH (first 280 chars):**")
                st.code(body_after[:280] if body_after else "(none)")
                comp = debug.get("comparison") or {}
                col_a, col_b = st.columns(2)
                col_a.metric("subject_match", str(comp.get("subject_match")))
                col_b.metric("body_match", str(comp.get("body_match")))
                st.write("20. **Verdict:**")
                if comp.get("subject_match") and comp.get("body_match"):
                    st.success(debug.get("verdict") or "match")
                else:
                    st.error(debug.get("verdict") or "mismatch")

                with st.expander("Raw diagnostic dict (everything above, structured)"):
                    st.json(debug)
