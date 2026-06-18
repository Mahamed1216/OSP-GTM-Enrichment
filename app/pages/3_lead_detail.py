"""Lead Detail — header + 5 tabs covering the full pipeline footprint per lead."""
from __future__ import annotations

import sys
import time
from html import escape
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from app.lib.badges import pill, status_badge, tier_badge
from app.lib.components import fit_score_viz
from app.lib.db_queries import get_lead_full
from app.lib.workspace_state import get_current_workspace_id, render_workspace_banner
from app.lib.formatters import fmt_duration_ms, fmt_timestamp, source_status_display
from app.lib.rating_runner import (
    delete_lead_sync,
    full_refresh_sync,
    record_rating_sync,
    regenerate_direct_sync,
    regenerate_email_sync,
    regenerate_sync,
    rerun_enrichment_sync,
    rerun_scoring_sync,
    research_hiring_signal_sync,
)
from app.styles import inject_styles
from src.feedback.ratings import get_rating

inject_styles()

_KIND_LABELS = {
    "email": "Email",
    "call_script": "Call Script",
    "linkedin_msg": "LinkedIn DM",
}
_PAYLOAD_COLUMNS = [
    "linkedin_profile",
    "company_details",
    "company_news",
    "industry_news",
]


def _walk_chain(contents_for_kind: list[dict], head_id: int, max_depth: int = 50) -> list[dict]:
    """Return older versions newest-superseded first, walking back via superseded_by_id.

    Defense: seen-set + depth cap so malformed data (cycle, runaway chain) cannot
    loop the UI render. Backend refuses to write a cycle in normal operation
    because regenerate rejects sources whose superseded_by_id is already set.
    """
    older: list[dict] = []
    seen: set[int] = {head_id}
    cursor = head_id
    while len(older) < max_depth:
        prev = next(
            (c for c in contents_for_kind if c.get("superseded_by_id") == cursor),
            None,
        )
        if prev is None:
            break
        if prev["id"] in seen:
            import logging
            logging.getLogger(__name__).warning(
                "lead_detail_chain_cycle_detected",
                extra={"head_id": head_id, "cursor": cursor, "prev_id": prev["id"]},
            )
            break
        older.append(prev)
        seen.add(prev["id"])
        cursor = prev["id"]
    return older


def _render_rating_block(content_id: int) -> None:
    """Show either a 'rated' badge or the up/down + feedback + submit widgets.

    Keys are scoped by content_id so the three sub-tabs (email/call/DM) don't
    collide. Re-rating is silently disallowed by hiding the widgets after a
    rating exists — backend defense-in-depth raises RatingAlreadyExistsError
    if force-called.
    """
    try:
        rating = get_rating(content_id)
    except Exception as exc:
        st.error(f"Could not load rating state: {exc}")
        return

    st.divider()

    if rating is not None:
        emoji = "👍" if rating["rating"] == "up" else "👎"
        st.markdown(
            f"**Rated {emoji}** by `{rating['rated_by']}` · "
            f"{fmt_timestamp(rating['rated_at'])}"
        )
        if rating.get("feedback_text"):
            st.markdown(f"> {rating['feedback_text']}")
        return

    st.markdown("**Rate this content**")
    choice_key = f"ld_rate_choice_{content_id}"
    fb_key = f"ld_rate_fb_{content_id}"
    submit_key = f"ld_rate_submit_{content_id}"

    rc, fbc = st.columns([1, 3])
    with rc:
        choice = st.segmented_control(
            "Rating",
            options=["👍 Up", "👎 Down"],
            key=choice_key,
            label_visibility="collapsed",
        )
    with fbc:
        feedback = st.text_input(
            "Why? (optional)",
            key=fb_key,
            placeholder="e.g. too generic, missed the signal",
            label_visibility="collapsed",
        )

    if st.button(
        "Submit rating",
        key=submit_key,
        type="primary",
        disabled=choice is None,
    ):
        rating_value = "up" if choice == "👍 Up" else "down"
        try:
            with st.spinner("Recording rating…"):
                record_rating_sync(content_id, rating_value, feedback)
            st.toast(f"Rating saved: {choice}", icon="✅")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save rating: {exc}")


_ws_id = get_current_workspace_id()

# ---------- Guard: must have a selected lead ----------
selected_id = st.session_state.get("selected_lead_id")
if selected_id is None:
    st.title("Lead Detail")
    st.info("No lead selected. Pick one from the Leads page.")
    if st.button("Go to Leads", key="ld_go_leads"):
        st.switch_page("pages/2_leads.py")
    st.stop()

# ---------- Load ----------
try:
    bundle = get_lead_full(int(selected_id), workspace_id=_ws_id)
except Exception as exc:
    st.error(f"Could not load lead {selected_id}: {exc}")
    st.stop()

if bundle is None:
    st.warning(
        f"Lead {selected_id} not found in the current workspace. "
        "It may belong to a different workspace."
    )
    if st.button("Back to Leads", key="ld_back_missing"):
        st.switch_page("pages/2_leads.py")
    st.stop()

lead = bundle["lead"]
enrichment = bundle["enrichment"]
score = bundle["score"]
contents = bundle["contents"]
engagements = bundle["engagements"]
hiring_signal = bundle.get("hiring_signal")
source_signal = bundle.get("source_signal")

render_workspace_banner()

# ---------- Header ----------
if st.button("← Back", key="ld_back_btn"):
    st.switch_page("pages/2_leads.py")

name = f"{lead['first_name']} {lead['last_name']}".strip() or "Lead"
company_line = lead.get("company") or "—"
title_line = lead.get("title") or "—"
st.markdown(
    f'<div style="margin-bottom: 3rem;">'
    f'<h1 class="hero-headline" style="font-size: 64px;">{escape(name)}.</h1>'
    f'<p class="hero-sublabel">{escape(title_line)} at {escape(company_line)}</p>'
    f'</div>',
    unsafe_allow_html=True,
)

m1, m2, m3 = st.columns([1, 1, 4])
with m1:
    st.markdown(f"**Tier**  {tier_badge(score['tier'] if score else None)}")
with m2:
    st.markdown(
        f"**Score**  {score['score']}" if score else "**Score**  —"
    )
with m3:
    if lead.get("email"):
        st.markdown(f"**Email**  `{lead['email']}`")

# ICP-skip banner. Tier "D" is reserved for confirmed B2C / non-fit
# leads — scoring writes it via the deterministic rule:b2c_skip path.
# The banner sits between the metric row and the actions panel so the
# operator sees it before deciding to rerun anything.
if score and score.get("tier") == "D":
    st.warning(
        "**B2C company. Not a fit for OSP unless clear B2B sales "
        "motion exists.**  \n"
        + (score.get("rationale") or "")
    )

st.divider()

# ---------- Lead actions ----------
# Compact panel: rerun enrichment / scoring / email / full refresh for
# THIS lead without bouncing back to Run Pipeline. Each button tracks
# its own in-flight flag so a failure can't permanently disable the row.
# Buttons run synchronously inside the click handler (the underlying
# helpers wrap async coroutines via run_async); on completion we rerun
# the page so the tabs below reflect fresh data without a manual refresh.
st.markdown("**Lead actions**")
_action_cols = st.columns(4)
_action_lead_id = int(lead["id"])

_REENRICH_KEY = f"ld_action_enrich_{_action_lead_id}"
_RESCORE_KEY = f"ld_action_score_{_action_lead_id}"
_REGEN_KEY = f"ld_action_regen_{_action_lead_id}"
_FULL_KEY = f"ld_action_full_{_action_lead_id}"


def _run_with_spinner(label: str, fn, *args, **kwargs):
    """Run a sync helper with a spinner + standardized error capture.

    Returns (ok, result_or_error). On failure, the exact exception text
    is propagated to the caller so the UI shows it via st.error.
    """
    try:
        with st.spinner(label):
            return True, fn(*args, **kwargs)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


with _action_cols[0]:
    if st.button("Rerun enrichment", key=_REENRICH_KEY, type="secondary"):
        ok, result = _run_with_spinner(
            "Rerunning enrichment…", rerun_enrichment_sync, _action_lead_id,
            workspace_id=_ws_id,
        )
        if not ok:
            st.error(f"Enrichment failed: {result}")
        else:
            errored = [
                name for name, meta in (result or {}).items()
                if meta.get("status") == "error"
            ]
            if errored:
                st.warning(
                    "Enrichment completed with errors in: " + ", ".join(errored)
                    + ". Open the Enrichment tab for details."
                )
            else:
                st.success("Enrichment rerun complete.")
            st.rerun()

with _action_cols[1]:
    if st.button("Rerun scoring", key=_RESCORE_KEY, type="secondary"):
        ok, result = _run_with_spinner(
            "Rerunning scoring…", rerun_scoring_sync, _action_lead_id,
            workspace_id=_ws_id,
        )
        if not ok:
            st.error(f"Scoring failed: {result}")
        else:
            st.success(
                f"Scored {result['score']} ({result['tier']})."
            )
            st.rerun()

with _action_cols[2]:
    if st.button("Regenerate email", key=_REGEN_KEY, type="secondary"):
        ok, result = _run_with_spinner(
            "Regenerating email…", regenerate_email_sync, _action_lead_id,
            workspace_id=_ws_id,
        )
        if not ok:
            st.error(f"Email regeneration failed: {result}")
        else:
            # Point the email-tab head pointer at the new row so the
            # Generated Content tab shows the fresh version without
            # the operator selecting it manually.
            st.session_state[f"ld_version_{_action_lead_id}_email"] = int(result)
            st.success("Email regenerated with latest prompt.")
            st.rerun()

with _action_cols[3]:
    if st.button("Run full refresh", key=_FULL_KEY, type="primary"):
        ok, result = _run_with_spinner(
            "Full refresh: enrichment → scoring → email…",
            full_refresh_sync, _action_lead_id,
            workspace_id=_ws_id,
        )
        if not ok:
            # _run_with_spinner already collapsed the exception.
            st.error(f"Full refresh failed: {result}")
        elif isinstance(result, dict) and result.get("failed_step"):
            st.error(
                f"Full refresh failed at step '{result['failed_step']}': "
                f"{result.get('error', 'unknown error')}"
            )
            st.rerun()
        else:
            new_email = result.get("email_id") if isinstance(result, dict) else None
            if new_email:
                st.session_state[
                    f"ld_version_{_action_lead_id}_email"
                ] = int(new_email)
            scoring = (result or {}).get("scoring") or {}
            score_bit = (
                f" · score {scoring['score']} ({scoring['tier']})"
                if "score" in scoring
                else ""
            )
            # Stash per-step status so it survives the rerun below and is
            # rendered as a "Last pipeline run" panel (requirement: pipeline
            # run logs must be visible, including hiring signal enrichment).
            st.session_state[f"ld_last_refresh_{_action_lead_id}"] = (result or {}).get("steps") or {}
            st.success(
                f"Full refresh complete{score_bit}. "
                "No Instantly push, no campaign activation."
            )
            st.rerun()

# ---------- Last pipeline run panel ----------
# Renders the per-step status from the most recent "Run full refresh" so the
# operator can confirm every step ran — including hiring signal enrichment.
_last_refresh = st.session_state.get(f"ld_last_refresh_{_action_lead_id}")
if _last_refresh:
    with st.container(border=True):
        st.markdown("**Last pipeline run**")
        _glyph = {"completed": "✅", "skipped": "⏭", "failed": "❌"}
        _order = [
            ("enrichment", "Enrichment"),
            ("buyer_research", "Buyer research"),
            ("hiring_signal", "Hiring signal enrichment"),
            ("scoring", "Scoring"),
            ("content", "Content generation"),
        ]
        for k, label in _order:
            if k in _last_refresh:
                st.markdown(f"{_glyph.get(_last_refresh[k], '•')} {label}: `{_last_refresh[k]}`")

st.divider()

# ---------- Tabs ----------
tab_lead, tab_enrich, tab_score, tab_content, tab_delivery = st.tabs(
    [
        "Original Lead",
        "Enrichment",
        "Scoring",
        "Generated Content",
        "Delivery & Engagement",
    ]
)

# === Tab 1: Original Lead ===
with tab_lead:
    rows = [
        ("ID", lead["id"]),
        ("First name", lead["first_name"]),
        ("Last name", lead["last_name"]),
        ("Email", lead["email"]),
        ("Title", lead["title"] or "—"),
        ("Company", lead["company"] or "—"),
        ("Company domain", lead["company_domain"] or "—"),
        ("Industry", lead["industry"] or "—"),
        ("LinkedIn (lead)", lead["linkedin_url"] or "—"),
        ("LinkedIn (company)", lead["company_linkedin_url"] or "—"),
        ("Email verification status", lead["email_verification_status"] or "—"),
        ("Email verification provider", lead["email_verification_provider"] or "—"),
        ("Email verified at", fmt_timestamp(lead["email_verified_at"])),
        ("Created at", fmt_timestamp(lead["created_at"])),
    ]
    for label, value in rows:
        c1, c2 = st.columns([1, 3])
        c1.markdown(f"**{label}**")
        c2.write(value)

    # ---------- Source Signal / Imported Enrichment ----------
    st.divider()
    st.subheader("Source Signal / Imported Enrichment")
    _raw = lead.get("lead_source_raw") or {}
    _has_any_source = bool(_raw) or source_signal is not None
    if not _has_any_source:
        st.caption("This lead was not imported from the OSP Lead Engine.")
    else:
        _src_strength = (source_signal or {}).get("signal_strength")
        _STRENGTH_COLOR = {"high": ":green", "medium": ":orange", "low": ":gray", "none": ":gray"}
        with st.container(border=True):
            sc1, sc2 = st.columns(2)
            with sc1:
                st.markdown(f"**Source system:** {lead.get('external_source') or '—'}")
                st.markdown(f"**External contact id:** `{lead.get('external_contact_id') or '—'}`")
                st.markdown(f"**Source tier:** {lead.get('source_tier') or '—'}")
                _score = lead.get("source_tier_score")
                st.markdown(f"**Source tier score:** {_score if _score is not None else '—'}")
            with sc2:
                st.markdown(f"**Enrichment status:** {_raw.get('enrichment_status') or '—'}")
                _ev = (
                    "verified" if _raw.get("email_verified")
                    else (lead.get("email_verification_status") or "—")
                )
                st.markdown(f"**Email verification:** {_ev}")
                if _raw.get("email_bounce_risk") is not None:
                    st.markdown(f"**Email bounce risk:** {_raw.get('email_bounce_risk')}")
                _icps = (source_signal or {}).get("matched_icps") or _raw.get("matched_icps") or []
                if _icps:
                    st.markdown(
                        "**Matched ICPs:** "
                        + ", ".join(str(i) for i in (_icps if isinstance(_icps, list) else [_icps]))
                    )

            # Signals
            if source_signal and source_signal.get("signal_found"):
                color = _STRENGTH_COLOR.get((_src_strength or "none").lower(), ":gray")
                st.markdown(
                    f"**Signal strength:** {color}[**{(_src_strength or 'unknown').title()}**]"
                )
                names = source_signal.get("signal_names") or []
                if names:
                    st.markdown("**Signals:** " + "  ".join(pill(str(n), "violet") for n in names))
                if source_signal.get("summary"):
                    st.markdown(f"**Signal summary:** {source_signal['summary']}")
                if source_signal.get("recommended_email_angle"):
                    st.markdown(
                        f"**Suggested angle:** _{source_signal['recommended_email_angle']}_"
                    )
                _surls = source_signal.get("source_urls") or []
                if _surls:
                    with st.expander(f"Signal source URLs ({len(_surls)})", expanded=False):
                        for u in _surls:
                            st.markdown(f"- {u}")
            else:
                st.info("No source signals were included in the imported payload.")

            # Source metadata
            _meta = _raw.get("source_metadata")
            if _meta:
                with st.expander("Source metadata", expanded=False):
                    st.json(_meta)

            # Full raw payload (audit)
            if _raw:
                with st.expander("Raw imported payload (JSON)", expanded=False):
                    st.json(_raw)

# === Tab 2: Enrichment ===
with tab_enrich:
    if enrichment is None:
        st.info("This lead has not been enriched yet.")
    else:
        st.caption(f"Last enriched: {fmt_timestamp(enrichment['enriched_at'])}")
        source_status = enrichment.get("source_status") or {}

        if not source_status:
            st.info("No source status recorded.")
        else:
            cols = st.columns(2)
            for idx, (source, info) in enumerate(source_status.items()):
                info = info or {}
                icon, label = source_status_display(info)
                status = info.get("status") or (
                    "ok" if info.get("success") else "error"
                )
                with cols[idx % 2]:
                    with st.container(border=True):
                        st.markdown(
                            f"{icon} **{source}**  &nbsp;·&nbsp; "
                            f"_{label}_  &nbsp;·&nbsp; "
                            f"{fmt_duration_ms(info.get('duration_ms'))}"
                        )
                        if status == "error":
                            err = info.get("error") or "unknown error"
                            st.markdown(f":red[**Error:**] `{err}`")
                        elif status in ("no_results", "skipped"):
                            reason = info.get("reason") or "no payload returned"
                            st.markdown(f":orange[**{label.title()}:**] {reason}")

        # ---------- Buyer account research card ----------
        st.subheader("Buyer account research")
        ba = enrichment.get("buyer_accounts") or {}
        if not ba:
            st.info(
                "No buyer-account research stored yet. Click Lead Actions "
                "→ Rerun enrichment to populate this."
            )
        else:
            motion = ba.get("buyer_motion") or "unknown"
            # v3 fallback ladder fields
            fallback_mode = ba.get("buyer_fallback_mode") or ""
            v3_direct = ba.get("direct_buyer_accounts") or []
            v3_lookalike = ba.get("lookalike_buyer_accounts") or []
            v3_trigger = ba.get("trigger_based_buyer_segments") or []
            research_status = ba.get("buyer_research_status") or ""
            research_conf = ba.get("buyer_research_confidence") or ""
            research_rationale = ba.get("buyer_research_rationale") or ""
            # v2 / legacy fields
            direct_buyers = ba.get("likely_direct_buyers") or []
            partner_channels = ba.get("likely_partner_channels") or []
            segments = ba.get("likely_buyer_segments") or []
            buyer_conf = (
                ba.get("buyer_account_confidence")
                or ba.get("buyer_confidence")
                or "low"
            )
            partner_conf = ba.get("partner_confidence") or "low"
            rationale = ba.get("buyer_account_rationale") or ba.get("reasoning") or ""
            flagged = ba.get("flagged_competitors") or []
            b2b_evidence = ba.get("explicit_b2b_motion_evidence") or []

            # needs_review warning (v3 rows only)
            if fallback_mode == "needs_review" or research_status == "needs_review":
                st.warning(
                    "No direct buyer account, lookalike buyer account, or trigger "
                    "based buyer segment found. Review before generating outreach."
                )

            with st.container(border=True):
                # Fallback mode row (v3 rows)
                if fallback_mode:
                    _MODE_LABELS = {
                        "direct_accounts": "Direct buyer accounts",
                        "direct_plus_lookalike": "Direct buyer + lookalike",
                        "lookalike_accounts": "Lookalike buyer accounts",
                        "trigger_segment": "Trigger-based segment",
                        "needs_review": "Needs buyer research",
                    }
                    mode_label = _MODE_LABELS.get(fallback_mode, fallback_mode)
                    mode_color = (
                        ":red" if fallback_mode == "needs_review"
                        else ":orange" if fallback_mode == "trigger_segment"
                        else ":green"
                    )
                    st.markdown(
                        f"**buyer_fallback_mode:** {mode_color}[**{mode_label}**]"
                    )

                cols = st.columns(2)
                with cols[0]:
                    st.markdown(f"**Motion:** `{motion}`")
                    st.markdown(f"**Buyer confidence:** {buyer_conf}")
                    if research_conf:
                        st.markdown(f"**Research confidence:** {research_conf}")
                with cols[1]:
                    st.markdown(f"**Partner confidence:** {partner_conf}")
                    if research_status:
                        st.markdown(f"**Research status:** `{research_status}`")
                    if b2b_evidence:
                        st.markdown(
                            f"**B2B motion evidence ({len(b2b_evidence)}):** "
                            + ", ".join(b2b_evidence[:3])
                            + (" …" if len(b2b_evidence) > 3 else "")
                        )

                # Research date window + Tavily metadata (set by code).
                _nw = ba.get("news_window_days")
                if _nw:
                    _topic = ba.get("tavily_topic_used") or "—"
                    _rc = ba.get("result_count")
                    _modes = ba.get("tavily_modes_used") or []
                    st.caption(
                        f"News window: last {_nw} days "
                        f"({ba.get('news_start_date') or '?'} → {ba.get('news_end_date') or '?'}) "
                        f"· Tavily topic: {_topic}"
                        + (f" · {_rc} results" if _rc is not None else "")
                        + (f" · modes: {', '.join(_modes)}" if _modes else "")
                    )

                    # ----- Tavily Company Research (research agent) -----
                    st.markdown("**Tavily Company Research**")
                    _r_used = bool(ba.get("research_used"))
                    _r_status = ba.get("research_status") or "not_started"
                    _r_useful = bool(ba.get("research_useful_signal_found"))
                    if not _r_used:
                        st.info("Tavily Company Research has not run for this lead.")
                    elif _r_status == "failed":
                        st.error(
                            f"Tavily Company Research failed: {ba.get('research_error') or 'unknown error'}"
                        )
                    elif not _r_useful:
                        st.warning(
                            "Tavily Company Research completed, but no useful company "
                            "signal was found."
                            + (f"  \nReason: {ba.get('research_no_signal_reason')}"
                               if ba.get("research_no_signal_reason") else "")
                        )
                    else:
                        st.markdown(
                            f"Status: `{_r_status}` · Used: yes · Useful signal: **yes**"
                        )
                        if ba.get("research_summary"):
                            st.markdown(f"**Summary:** {ba['research_summary']}")
                        _rfind = ba.get("research_findings") or []
                        if _rfind:
                            st.markdown("**Findings:**")
                            for f in _rfind[:6]:
                                st.markdown(f"- {f}")
                        _rsrc = ba.get("research_sources") or []
                        if _rsrc:
                            with st.expander(f"Research source URLs ({len(_rsrc)})", expanded=False):
                                for u in _rsrc:
                                    st.markdown(f"- {u}")
                    if ba.get("research_last_run_at"):
                        st.caption(f"Research last run: {ba['research_last_run_at']}")

                    # Selected strongest signal (relevance-first, not newest).
                    _sel = ba.get("selected_signal")
                    if _sel:
                        _rel = (_sel.get("signal_relevance") or "?").title()
                        _age = _sel.get("signal_recency_days")
                        _age_str = f"~{_age}d ago" if _age is not None else "age unknown"
                        st.markdown(
                            f"**Strongest signal:** {_sel.get('signal_type') or 'signal'} "
                            f"({_rel} relevance · {_age_str}) — {_sel.get('signal_summary') or ''}"
                        )
                        if _sel.get("reason"):
                            st.caption(f"Why selected: {_sel['reason']}")
                        if _sel.get("source_url"):
                            st.caption(f"Source: {_sel['source_url']}")
                    elif _nw:
                        st.caption(
                            f":gray[No strong company signal found within the configured {_nw}-day window.]"
                        )

                    if ba.get("recommended_outreach_angle"):
                        st.markdown(
                            f"**Recommended outreach angle:** _{ba['recommended_outreach_angle']}_"
                        )

                    # All candidate signals (expandable).
                    _cands = ba.get("candidate_signals") or []
                    if _cands:
                        with st.expander(f"All candidate signals ({len(_cands)})", expanded=False):
                            for c in _cands:
                                _cage = c.get("signal_recency_days")
                                st.markdown(
                                    f"- **{c.get('signal_type') or 'signal'}** "
                                    f"({(c.get('signal_relevance') or '?')}, "
                                    f"{('~' + str(_cage) + 'd') if _cage is not None else 'age?'}): "
                                    f"{c.get('signal_summary') or ''}"
                                    + (f"  [{c.get('source_url')}]" if c.get('source_url') else "")
                                )

                    _surls = ba.get("source_urls") or []
                    if _surls:
                        with st.expander(f"Research source URLs ({len(_surls)})", expanded=False):
                            for u in _surls:
                                st.markdown(f"- {u}")

                    # Tavily call debug metadata — verify the date window per call.
                    _calls = ba.get("tavily_calls") or []
                    if _calls:
                        with st.expander(f"Tavily call debug metadata ({len(_calls)} calls)", expanded=False):
                            st.caption(
                                "Each buyer/company research Tavily call with its "
                                "actual parameters (start_date/end_date confirm the window)."
                            )
                            for call in _calls:
                                st.markdown(
                                    f"- **{call.get('mode')}** · topic=`{call.get('topic')}` · "
                                    f"depth=`{call.get('search_depth')}` · max={call.get('max_results')} · "
                                    f"start=`{call.get('start_date')}` end=`{call.get('end_date')}` "
                                    f"time_range=`{call.get('time_range')}` · "
                                    f"{call.get('result_count')} results "
                                    f"(oldest {call.get('oldest_result_date') or '?'}, "
                                    f"newest {call.get('newest_result_date') or '?'})"
                                )
                            st.json(_calls)

                # v3 fields (shown when present)
                if fallback_mode:
                    st.markdown(
                        "**direct_buyer_accounts:** "
                        + (", ".join(v3_direct) if v3_direct else ":gray[(none)]")
                    )
                    st.markdown(
                        "**lookalike_buyer_accounts:** "
                        + (", ".join(v3_lookalike) if v3_lookalike else ":gray[(none)]")
                    )
                    st.markdown(
                        "**trigger_based_buyer_segments:** "
                        + (", ".join(v3_trigger) if v3_trigger else ":gray[(none)]")
                    )
                    if research_rationale:
                        st.caption(f"Research rationale: {research_rationale}")

                # v2 fields (always shown)
                if direct_buyers:
                    st.markdown(
                        "**likely_direct_buyers:** " + ", ".join(direct_buyers)
                    )
                if partner_channels:
                    st.markdown(
                        "**likely_partner_channels:** " + ", ".join(partner_channels)
                    )
                if segments:
                    st.markdown(
                        "**likely_buyer_segments:** " + ", ".join(segments)
                    )
                if flagged:
                    st.markdown(
                        ":red[**flagged_competitors (never name as buyers):**] "
                        + ", ".join(flagged)
                    )
                if rationale:
                    st.caption(f"Rationale: {rationale}")

                # Explain why no named accounts for old rows (no fallback_mode)
                if not fallback_mode and not v3_direct:
                    legacy_accounts = ba.get("likely_buyer_accounts") or []
                    if not legacy_accounts:
                        if motion.upper() == "B2C" and not b2b_evidence:
                            st.warning(
                                "No named buyers because the company appears "
                                "B2C with no explicit B2B motion evidence — "
                                "this lead is the ICP-skip case (tier D)."
                            )
                        else:
                            st.warning(
                                "No 2 high-confidence named buyer accounts "
                                "surfaced. Email generation will fall back to "
                                "buyer segments + \"teams like that\" CTA."
                            )

        st.subheader("Source payloads")
        any_payload = False
        for col in _PAYLOAD_COLUMNS:
            payload = enrichment.get(col)
            label = col.replace("_", " ").title()
            if payload is None:
                with st.expander(f"{label}  ·  _no data_", expanded=False):
                    st.caption("Source returned no payload (failed or skipped).")
                continue
            any_payload = True
            count_hint = (
                f"{len(payload)} items"
                if isinstance(payload, list)
                else f"{len(payload)} keys"
                if isinstance(payload, dict)
                else ""
            )
            with st.expander(f"{label}  ·  {count_hint}", expanded=False):
                st.json(payload)
        if not any_payload:
            st.caption("No source returned a usable payload for this lead.")

# === Tab 3: Scoring ===
with tab_score:
    if score is None:
        st.info("This lead has not been scored yet.")
    else:
        st.markdown(
            fit_score_viz(score=score["score"], tier=score["tier"]),
            unsafe_allow_html=True,
        )
        st.caption(
            f"Model: `{score['model']}`  ·  Scored at: {fmt_timestamp(score['scored_at'])}"
        )
        st.subheader("Rationale")
        st.write(score["rationale"])
        st.subheader("Signals used")
        signals = score.get("signals_used") or []
        if not signals:
            st.caption("No signals recorded.")
        else:
            chips = "  ".join(pill(s, "blue") for s in signals)
            st.markdown(chips)

    # ---------- Hiring Signal Enrichment (C-tier rescue layer) ----------
    st.divider()
    st.subheader("Hiring Signal Enrichment")
    _STRENGTH_COLOR = {"high": ":green", "medium": ":orange", "low": ":gray", "none": ":gray"}
    _STATUS_BADGE = {
        "completed": ":green[completed]",
        "skipped": ":gray[skipped]",
        "failed": ":red[failed]",
        "not_started": ":gray[not started]",
    }
    _UPLIFT_LABEL = {
        "none": "No uplift",
        "C_to_B": "C → B",
        "C_to_A": "C → A",
        "B_to_A": "B → A",
    }
    if hiring_signal is None:
        st.info("Hiring signal enrichment has not run for this lead.")
    else:
        status = (hiring_signal.get("status") or "completed").lower()
        last_run = hiring_signal.get("last_run_at") or hiring_signal.get("updated_at")
        with st.container(border=True):
            st.markdown(
                f"**Status:** {_STATUS_BADGE.get(status, status)}"
                f"  &nbsp;·&nbsp; **Last updated:** {fmt_timestamp(last_run)}"
            )
            if status == "failed" and hiring_signal.get("error"):
                st.markdown(f":red[**Error:**] `{hiring_signal['error']}`")

            found = bool(hiring_signal.get("signal_found"))
            st.markdown(f"**hiring_signal_found:** {found}")
            strength = (hiring_signal.get("signal_strength") or "none").lower()
            color = _STRENGTH_COLOR.get(strength, ":gray")
            st.markdown(f"**hiring_signal_strength:** {color}[**{strength.title()}**]")

            roles = hiring_signal.get("relevant_roles") or []
            if roles:
                st.markdown("**relevant_roles_found:** " + "  ".join(pill(r, "green") for r in roles))
            else:
                st.markdown("**relevant_roles_found:** :gray[(none)]")

            depts = hiring_signal.get("relevant_departments") or []
            st.markdown(
                "**relevant_departments:** "
                + (", ".join(depts) if depts else ":gray[(none)]")
            )
            st.markdown(
                f"**recency_estimate:** {hiring_signal.get('recency_estimate') or 'unknown'}"
            )

            applied = hiring_signal.get("applied_uplift") or "none"
            rec = hiring_signal.get("tier_uplift_recommendation") or "none"
            st.markdown(
                f"**tier_uplift_recommendation:** {_UPLIFT_LABEL.get(rec, rec)}"
                + (
                    f"  _(applied: {_UPLIFT_LABEL.get(applied, applied)})_"
                    if applied != rec else ""
                )
            )
            if hiring_signal.get("why_it_matters"):
                st.markdown(f"**why_it_matters:** {hiring_signal['why_it_matters']}")
            if hiring_signal.get("recommended_email_angle"):
                st.markdown(
                    f"**recommended_email_angle:** _{hiring_signal['recommended_email_angle']}_"
                )
            urls = hiring_signal.get("source_urls") or []
            if urls:
                with st.expander(f"source_urls ({len(urls)})", expanded=False):
                    for u in urls:
                        st.markdown(f"- {u}")

    if st.button("Run hiring signal enrichment", key=f"ld_hiring_{lead['id']}", type="secondary"):
        ok, result = _run_with_spinner(
            "Running hiring signal enrichment…",
            research_hiring_signal_sync,
            int(lead["id"]),
            workspace_id=_ws_id,
        )
        if not ok:
            st.error(f"Hiring signal enrichment failed: {result}")
        else:
            _status = (result or {}).get("status", "completed")
            st.success(
                f"Hiring signal enrichment {_status}. No email sent, no Instantly push."
            )
            st.cache_data.clear()
            st.rerun()

# === Tab 4: Generated Content ===
with tab_content:
    if not contents:
        st.info("No content has been generated for this lead yet.")
    else:
        # Identify head per kind (non-superseded, newest by id-desc ordering).
        heads_by_kind: dict[str, dict] = {}
        contents_by_kind: dict[str, list[dict]] = {}
        for c in contents:
            contents_by_kind.setdefault(c["kind"], []).append(c)
            if c.get("superseded_by_id") is None and c["kind"] not in heads_by_kind:
                heads_by_kind[c["kind"]] = c

        ordered_kinds = [k for k in ("email", "call_script", "linkedin_msg") if k in heads_by_kind]
        if not ordered_kinds:
            st.info("Content exists but kinds are unrecognized.")
        else:
            sub_tabs = st.tabs([_KIND_LABELS[k] for k in ordered_kinds])
            for tab, kind in zip(sub_tabs, ordered_kinds):
                with tab:
                    head = heads_by_kind[kind]
                    older = _walk_chain(contents_by_kind[kind], head["id"])

                    # Active version state — defaults to head; expander rows
                    # write into this key.
                    state_key = f"ld_version_{lead['id']}_{kind}"
                    active_id = st.session_state.get(state_key, head["id"])

                    # Resolve active row; fall back to head if the stored id is stale.
                    active = next(
                        (c for c in contents_by_kind[kind] if c["id"] == active_id),
                        head,
                    )
                    is_head = active["id"] == head["id"]

                    # Supersedes banner when viewing an older version.
                    if not is_head:
                        bcol1, bcol2 = st.columns([4, 1])
                        with bcol1:
                            st.warning(
                                f"⚠️ This version was superseded by a newer version "
                                f"generated on {fmt_timestamp(head['created_at'])}."
                            )
                        with bcol2:
                            if st.button(
                                "Show current",
                                key=f"ld_show_current_{kind}_{active['id']}",
                                type="primary",
                            ):
                                st.session_state[state_key] = head["id"]
                                st.rerun()

                    # Body rendering (same shape as before, but now keyed off `active`).
                    if active["subject"]:
                        st.markdown(f"**Subject:**  {active['subject']}")
                    body_height = max(180, min(600, len(active["body"]) // 2))
                    st.text_area(
                        "Body",
                        value=active["body"],
                        height=body_height,
                        disabled=True,
                        key=f"ld_body_{kind}_{active['id']}",
                    )
                    # Metadata caption — model, prompt version, fingerprint
                    # (first 8 of the SHA-prefix stored on the row),
                    # current overlay source ("Database" when an overlay
                    # row exists; otherwise the JSON/code fallback), and
                    # the created_at rendered in Eastern Time.
                    _fp = (active.get("prompt_fingerprint") or "")[:8] or "—"
                    _source_label = "—"
                    if active["kind"] == "email":
                        try:
                            from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY
                            from src.prompts.loader import get_effective_prompt_with_source
                            _, _source_key = get_effective_prompt_with_source(
                                "email", DEFAULT_EMAIL_PROMPT_BODY,
                            )
                            _source_label = {
                                "database": "Database",
                                "local_json": "Local JSON",
                                "code_default": "Code default",
                            }.get(_source_key, _source_key)
                        except Exception:
                            _source_label = "—"
                    st.caption(
                        f"Model: `{active['model']}`  ·  "
                        f"Prompt version: `{active['prompt_version']}`  ·  "
                        f"Fingerprint: `{_fp}`  ·  "
                        f"Prompt source: {_source_label}  ·  "
                        f"Created: {fmt_timestamp(active['created_at'])}"
                    )
                    cited = active.get("signals_cited") or []
                    if cited:
                        st.markdown(
                            "**Signals cited:**  " + "  ".join(pill(s, "violet") for s in cited)
                        )

                    if is_head:
                        # Rating widgets only on the head.
                        _render_rating_block(active["id"])

                        # Always-available "Redo with feedback" widget. Free
                        # text input + button; not gated on a rating. Calls
                        # `regenerate_direct_sync` which re-reads the LIVE
                        # DB prompt overlay before generating, so the new
                        # row picks up any prompt edits the operator just
                        # saved on the Prompts page. The old row's
                        # superseded_by_id is set; the head pointer moves.
                        st.divider()
                        st.markdown("**Redo with feedback**")
                        redo_fb_key = f"ld_redo_fb_{kind}_{active['id']}"
                        redo_btn_key = f"ld_redo_btn_{kind}_{active['id']}"
                        redo_running_key = f"ld_redo_running_{kind}_{active['id']}"
                        in_flight = bool(st.session_state.get(redo_running_key, False))
                        redo_feedback = st.text_area(
                            "What should the rewrite change?",
                            key=redo_fb_key,
                            placeholder=(
                                "e.g. use the founding SDR direct pitch; drop \"real tension\"; "
                                "no sender signature."
                            ),
                            disabled=in_flight,
                            height=80,
                        )
                        if st.button(
                            "🔄 Redo with feedback (uses latest DB prompt)",
                            key=redo_btn_key,
                            type="primary",
                            disabled=in_flight or not (redo_feedback or "").strip(),
                        ):
                            st.session_state[redo_running_key] = True
                            new_id: int | None = None
                            error_msg: str | None = None
                            try:
                                with st.spinner("Regenerating with feedback…"):
                                    new_id = regenerate_direct_sync(
                                        active["id"], redo_feedback,
                                    )
                            except Exception as exc:
                                error_msg = f"{type(exc).__name__}: {exc}"
                            finally:
                                # ALWAYS clear the in-flight flag, even on
                                # error — that's what was leaving the input
                                # permanently disabled.
                                st.session_state[redo_running_key] = False

                            if error_msg is not None:
                                st.error(f"Redo failed: {error_msg}")
                            elif new_id is not None:
                                st.session_state[state_key] = new_id
                                st.success("Email regenerated with latest prompt.")
                                st.rerun()

                        # Legacy rating-driven path still available when the
                        # operator has rated the row down with feedback.
                        # Kept as a secondary affordance so the existing
                        # workflow still works.
                        try:
                            head_rating = get_rating(active["id"])
                        except Exception:
                            head_rating = None
                        if (
                            head_rating
                            and head_rating.get("rating") == "down"
                            and (head_rating.get("feedback_text") or "").strip()
                        ):
                            if st.button(
                                "🔄 Regenerate using the saved rating feedback",
                                key=f"ld_regen_{kind}_{active['id']}",
                                type="secondary",
                            ):
                                try:
                                    with st.spinner("Regenerating with rating feedback…"):
                                        new_id = regenerate_sync(active["id"])
                                    st.toast("New version generated", icon="✨")
                                    st.session_state[state_key] = new_id
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Regenerate failed: {exc}")
                    else:
                        # On older versions: show their rating badge if any, no widgets.
                        try:
                            old_rating = get_rating(active["id"])
                        except Exception:
                            old_rating = None
                        if old_rating is not None:
                            st.divider()
                            emoji = "👍" if old_rating["rating"] == "up" else "👎"
                            st.markdown(
                                f"**This version was rated {emoji}** by `{old_rating['rated_by']}` · "
                                f"{fmt_timestamp(old_rating['rated_at'])}"
                            )
                            if old_rating.get("feedback_text"):
                                st.markdown(f"> {old_rating['feedback_text']}")

                    # Older-versions expander.
                    if older:
                        with st.expander(
                            f"📜 {len(older)} earlier version(s)", expanded=False
                        ):
                            for v in older:
                                row_cols = st.columns([5, 1])
                                with row_cols[0]:
                                    st.markdown(
                                        f"Version from {fmt_timestamp(v['created_at'])}  "
                                        f"`id={v['id']}`"
                                    )
                                with row_cols[1]:
                                    if v["id"] == active["id"]:
                                        st.caption("viewing")
                                    else:
                                        if st.button(
                                            "View",
                                            key=f"ld_view_{kind}_{v['id']}",
                                        ):
                                            st.session_state[state_key] = v["id"]
                                            st.rerun()

# === Tab 5: Delivery & Engagement ===
with tab_delivery:
    email_contents = [c for c in contents if c["kind"] == "email"]
    if not email_contents:
        st.info("No email has been generated yet, so there's nothing to deliver.")
    else:
        c = email_contents[0]
        d1, d2, d3 = st.columns(3)
        d1.markdown(
            f"**Delivered**  {status_badge(c['delivered_at'] is not None)}"
        )
        d2.markdown(f"**Provider**  `{c['delivery_provider'] or '—'}`")
        d3.markdown(f"**Delivery ID**  `{c['delivery_id'] or '—'}`")
        if c["delivered_at"]:
            st.caption(f"Delivered at: {fmt_timestamp(c['delivered_at'])}")
        if c["skip_reason"]:
            st.warning(f"Skipped — reason: `{c['skip_reason']}`")

        # Phase 9b: surface delivery errors with a one-click reset.
        if c.get("delivery_status") == "error":
            with st.container(border=True):
                st.markdown("**Delivery error**")
                st.caption(f"Last attempt failed: `{c.get('error_message') or 'no detail'}`")
                _reset_key = f"reset_delivery_pending_{c['id']}"
                pending_at = st.session_state.get(_reset_key)
                if pending_at and (time.time() - pending_at) < 5.0:
                    if st.button(
                        "Confirm reset",
                        type="primary",
                        key=f"confirm_reset_{c['id']}",
                    ):
                        from src.leads import clear_delivery_error
                        result = clear_delivery_error(c["id"])
                        st.session_state.pop(_reset_key, None)
                        if result.get("success"):
                            st.toast("Delivery state reset.")
                        else:
                            st.toast(f"Reset failed: {result.get('reason', 'unknown')}")
                        st.cache_data.clear()
                        st.rerun()
                    st.caption("Click within 5s to confirm.")
                else:
                    if st.button(
                        "Reset delivery state",
                        type="secondary",
                        key=f"reset_delivery_{c['id']}",
                    ):
                        st.session_state[_reset_key] = time.time()
                        st.rerun()

    st.subheader("Engagement events")
    if not engagements:
        st.info("No engagement events synced yet.")
    else:
        for e in engagements:
            with st.container(border=True):
                cols = st.columns(6)
                cols[0].markdown(f"**Sent**  {status_badge(e['sent'])}")
                cols[1].markdown(f"**Delivered**  {status_badge(e['delivered'])}")
                cols[2].markdown(f"**Opened**  {status_badge(e['opened'])}")
                cols[3].markdown(f"**Clicked**  {status_badge(e['clicked'])}")
                cols[4].markdown(f"**Replied**  {status_badge(e['replied'])}")
                cols[5].markdown(f"**Bounced**  {status_badge(e['bounced'])}")
                if e["reply_sentiment"]:
                    st.caption(f"Reply sentiment: **{e['reply_sentiment']}**")
                st.caption(f"Synced at: {fmt_timestamp(e['synced_at'])}")
                if e["raw"]:
                    with st.expander("Raw provider payload", expanded=False):
                        st.json(e["raw"])

# ---------- Danger zone: delete lead ----------
# Lives outside all tabs at the bottom of the page so it can't be hit
# accidentally. Two-step confirm; the pending state expires on the next
# interaction after PENDING_TTL seconds (Streamlit only re-renders on
# user interaction, so a true wall-clock auto-revert isn't possible).
PENDING_TTL = 10.0

st.divider()
with st.container(border=True):
    st.markdown(":red[**Danger zone**]")

    pending_key = f"ld_delete_pending_{lead['id']}"
    pending_at_key = f"ld_delete_pending_at_{lead['id']}"

    pending = bool(st.session_state.get(pending_key, False))
    pending_at = float(st.session_state.get(pending_at_key, 0.0))
    if pending and (time.monotonic() - pending_at) > PENDING_TTL:
        pending = False
        st.session_state[pending_key] = False
        st.caption(":gray[Previous delete confirmation expired.]")

    if not pending:
        st.caption(
            "Permanently delete this lead and every associated row "
            "(enrichment, score, content, ratings, engagement)."
        )
        if st.button(
            "Delete lead",
            type="secondary",
            key=f"ld_delete_init_{lead['id']}",
        ):
            st.session_state[pending_key] = True
            st.session_state[pending_at_key] = time.monotonic()
            st.rerun()
    else:
        if st.button(
            "⚠️ Click again to confirm deletion — this cannot be undone",
            type="primary",
            key=f"ld_delete_confirm_{lead['id']}",
        ):
            try:
                with st.spinner("Deleting lead…"):
                    result = delete_lead_sync(int(lead["id"]))
            except Exception as exc:
                st.error(f"Delete failed: {exc}")
                st.session_state.pop(pending_key, None)
                st.session_state.pop(pending_at_key, None)
            else:
                st.session_state.pop(pending_key, None)
                st.session_state.pop(pending_at_key, None)
                if result.get("success"):
                    counts = ", ".join(
                        f"{k}: {v}" for k, v in result["deleted_counts"].items()
                    )
                    st.success(
                        f"Lead {result['lead_id']} deleted ({counts})."
                    )
                    st.session_state.pop("selected_lead_id", None)
                    # Invalidate the 30-second cache on _list_leads_cached
                    # so the deleted row disappears on redirect.
                    st.cache_data.clear()
                    st.switch_page("pages/2_leads.py")
                else:
                    st.error(
                        f"Could not delete lead: {result.get('reason', 'unknown')}"
                    )
