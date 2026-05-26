"""Run Pipeline — CSV ingest + four-phase pipeline run with live st.status."""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.lib.db_queries import (
    get_all_lead_ids,
    get_content_lead_ids,
    get_content_pending_lead_ids,
    get_leads_with_content_by_tier,
    get_scored_lead_ids,
    get_unenriched_lead_ids,
    get_unscored_lead_ids,
    kpi_counts,
    list_leads,
)
from app.lib.ingest_runner import (
    IngestResult,
    auto_detect_and_validate,
    run_ingest_subprocess,
    save_upload_to_tempfile,
)
from app.lib.pipeline_runner import (
    PhaseUpdate,
    _send_eligible_lead_ids,
    _summary,
    bulk_regenerate_content,
    process_single_lead,
)
from app.styles import inject_styles
from src.ingest_aliases import (
    CANONICAL_ALIASES,
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    validate_mapping,
)

inject_styles()

_PHASE_LABELS = {
    "enrichment": "1. Enrichment",
    "scoring": "2. Scoring",
    "content": "3. Content",
    "delivery": "4. Delivery",
}

_SKIP_LABEL = "— Skip —"
_TARGET_OPTIONS = [_SKIP_LABEL] + REQUIRED_FIELDS + OPTIONAL_FIELDS


def parse_lead_ids(raw: str) -> list[int]:
    """Parse comma-separated IDs or a range like '177-203'."""
    raw = raw.strip()
    if not raw:
        return []
    range_match = re.match(r"^(\d+)-(\d+)$", raw)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if start > end:
            raise ValueError("Range start must be less than or equal to end.")
        if end - start > 500:
            raise ValueError("Range too large (max 500 leads at once).")
        return list(range(start, end + 1))
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise ValueError(
            "Invalid format: use integers separated by commas, or a range like '177-203'."
        )


def _run_pipeline_thread(
    lead_ids: list[int],
    dry_run: bool,
    run_enrichment: bool,
    run_scoring: bool,
    run_email: bool,
    run_call_script: bool,
    run_linkedin_msg: bool,
    progress_dict: dict,
) -> None:
    """Background-thread driver: walks lead_ids and updates a shared dict.

    Mutates ``progress_dict`` in place — the main Streamlit thread polls
    that dict on each rerun. We never touch ``st.session_state`` from
    here (no ScriptRunContext is attached to this thread).

    ``on_update`` is intentionally ``None`` because the Streamlit status
    callbacks aren't safe across thread boundaries either; per-lead
    progress flows through ``progress_dict['current']`` instead.
    """
    def _collect(u: PhaseUpdate) -> None:
        # Mutates the shared dict — safe because we never call any
        # Streamlit API from here, just a regular list.append().
        if not u.ok:
            progress_dict["errors"].append(
                f"Lead {u.lead_id} ({u.phase}): {u.error}"
            )

    try:
        total = len(lead_ids)
        progress_dict["total"] = total
        for i, lead_id in enumerate(lead_ids):
            progress_dict["current"] = i + 1
            progress_dict["current_lead"] = str(lead_id)
            try:
                process_single_lead(
                    lead_id,
                    dry_run=dry_run,
                    run_enrichment=run_enrichment,
                    run_scoring=run_scoring,
                    run_email=run_email,
                    run_call_script=run_call_script,
                    run_linkedin_msg=run_linkedin_msg,
                    on_update=_collect,
                )
            except Exception as exc:
                progress_dict["errors"].append(
                    f"Lead {lead_id}: {type(exc).__name__}: {exc}"
                )
        try:
            progress_dict["summary"] = _summary(lead_ids)
        except Exception as exc:
            progress_dict["errors"].append(f"Summary failed: {exc}")
    except Exception as exc:
        progress_dict["errors"].append(f"Pipeline failed: {exc}")
    finally:
        progress_dict["done"] = True


st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Pipeline.</h1>'
    '<p class="hero-sublabel">Ingest, enrich, score, generate, deliver.</p>'
    '</div>',
    unsafe_allow_html=True,
)

# =========================================================
# Section A — CSV ingest
# =========================================================
st.header("1) Ingest CSV")
st.caption(
    "Upload a CSV from Apollo, ZoomInfo, Sales Navigator, Lusha, or a "
    "snake_case export. Columns are auto-detected against the canonical "
    f"schema ({', '.join(REQUIRED_FIELDS)} required). "
    "Confirm or remap below before ingesting. Ingest is idempotent on email."
)

uploaded = st.file_uploader(
    "Leads CSV",
    type=["csv"],
    key="ingest_csv",
)

if "ingest_mapping_overrides" not in st.session_state:
    st.session_state["ingest_mapping_overrides"] = {}

ingest_ready = False
final_mapping: dict[str, str | None] | None = None
saved_csv_path: Path | None = None

if uploaded is not None:
    # ---------- preview ----------
    try:
        preview = pd.read_csv(uploaded, nrows=5)
        uploaded.seek(0)
        st.write(f"Preview ({len(preview)} of first rows):")
        st.dataframe(preview, hide_index=True, use_container_width=True, key="ingest_preview")
    except Exception as exc:
        st.error(f"Could not read CSV: {exc}")
        st.stop()

    # ---------- save tempfile + auto-detect ----------
    try:
        saved_csv_path = save_upload_to_tempfile(uploaded)
        csv_columns, auto_mapping, _, _ = auto_detect_and_validate(saved_csv_path)
    except Exception as exc:
        st.error(f"Could not parse CSV header: {exc}")
        st.stop()

    # Reverse: source_column -> canonical_field (from auto-detect)
    auto_source_to_canonical: dict[str, str] = {
        src: field for field, src in auto_mapping.items() if src
    }

    # Per-file session-state bucket so reruns don't lose user overrides.
    overrides_bucket: dict[str, str] = st.session_state["ingest_mapping_overrides"].setdefault(
        uploaded.name, {}
    )

    # ---------- mapping table UI ----------
    st.subheader("Column mapping")
    st.caption(
        "Each CSV column is shown with its auto-detected target. Use the "
        "dropdown to remap or skip. Canonical fields not listed in any "
        "dropdown (because no column maps to them) stay empty."
    )

    # current per-source choice: override > auto-detect > Skip
    current_choice: dict[str, str] = {}
    for src_col in csv_columns:
        if src_col in overrides_bucket:
            current_choice[src_col] = overrides_bucket[src_col]
        elif src_col in auto_source_to_canonical:
            current_choice[src_col] = auto_source_to_canonical[src_col]
        else:
            current_choice[src_col] = _SKIP_LABEL

    # Render rows
    for src_col in csv_columns:
        c1, c2 = st.columns([2, 2])
        c1.markdown(f"`{src_col}`")
        choice = c2.selectbox(
            label=f"Map {src_col} to",
            options=_TARGET_OPTIONS,
            index=_TARGET_OPTIONS.index(current_choice[src_col])
            if current_choice[src_col] in _TARGET_OPTIONS
            else 0,
            key=f"map_{uploaded.name}_{src_col}",
            label_visibility="collapsed",
        )
        overrides_bucket[src_col] = choice

    # ---------- build canonical_field -> source_column mapping ----------
    # First selection per canonical field wins; later collisions are dropped
    # at the canonical level (the user can re-pick to resolve).
    final_mapping = {field: None for field in CANONICAL_ALIASES}
    collisions: dict[str, list[str]] = {}
    for src_col in csv_columns:
        target = overrides_bucket.get(src_col, _SKIP_LABEL)
        if target == _SKIP_LABEL:
            continue
        if final_mapping[target] is None:
            final_mapping[target] = src_col
        else:
            collisions.setdefault(target, [final_mapping[target]]).append(src_col)

    # ---------- validation panel ----------
    st.subheader("Required fields")
    ok, errors = validate_mapping(final_mapping)
    for field in REQUIRED_FIELDS:
        src = final_mapping.get(field)
        if src:
            st.success(f"✓ `{field}` ← `{src}`")
        else:
            st.error(f"✗ `{field}` — pick a source column above.")

    mapped_optional = [
        f for f in OPTIONAL_FIELDS if final_mapping.get(f)
    ]
    if mapped_optional:
        st.caption(
            "Optional fields mapped: "
            + ", ".join(f"`{f}` ← `{final_mapping[f]}`" for f in mapped_optional)
        )

    if collisions:
        st.warning(
            "Multiple columns picked the same target — only the first is used. "
            "Re-map the extras to `— Skip —` or another field:"
        )
        for field, srcs in collisions.items():
            st.write(f"- `{field}` got: " + ", ".join(f"`{s}`" for s in srcs))

    ingest_ready = ok

if st.button(
    "Ingest",
    type="primary",
    disabled=uploaded is None or not ingest_ready or saved_csv_path is None,
    key="ingest_btn",
):
    # Snapshot existing IDs so we can diff and surface newly-inserted ones.
    try:
        existing_ids_before = set(get_all_lead_ids())
    except Exception:
        existing_ids_before = set()

    try:
        with st.spinner("Running scripts/ingest_leads.py …"):
            result: IngestResult = run_ingest_subprocess(
                saved_csv_path,
                mapping=final_mapping,
            )
    except Exception as exc:
        st.error(f"Ingest failed before subprocess: {exc}")
    else:
        if result.ok:
            st.success(
                f"Ingested CSV — inserted={result.inserted}, "
                f"updated={result.updated}, deduped={result.deduped_within_file}, "
                f"skipped={result.skipped}"
            )
            if result.skip_reasons:
                breakdown = ", ".join(
                    f"`{k}`={v}" for k, v in sorted(result.skip_reasons.items())
                )
                st.caption(f"Skip reasons: {breakdown}")
            st.cache_data.clear()

            try:
                all_ids_after = set(get_all_lead_ids())
            except Exception:
                all_ids_after = set()
            new_ids = sorted(all_ids_after - existing_ids_before)
            if new_ids:
                st.session_state["just_ingested_ids"] = new_ids
                st.session_state["just_ingested_count"] = len(new_ids)
        else:
            st.error(f"Ingest subprocess exited with code {result.returncode}")

        with st.expander("Subprocess output", expanded=not result.ok):
            if result.stdout:
                st.code(result.stdout, language="text")
            if result.stderr:
                st.code(result.stderr, language="text")

# Post-ingest CTA: surface the freshly-inserted IDs and offer a one-click
# hand-off into section 2's lead-IDs field.
_just_ids = st.session_state.get("just_ingested_ids")
if _just_ids:
    _just_n = st.session_state.get("just_ingested_count", len(_just_ids))
    st.markdown(
        '<div style="background:var(--cobalt);border-radius:8px;'
        'padding:20px 24px;margin:16px 0;">'
        '<div style="font-family:\'Fraunces\',serif;font-size:24px;'
        'font-weight:400;color:white;margin-bottom:6px;">'
        f'{_just_n} new leads ready to enrich.'
        '</div>'
        '<div style="font-size:14px;color:rgba(255,255,255,0.8);">'
        'These leads were just ingested and have no enrichment yet.'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    cta_col, dismiss_col = st.columns([1, 3])
    with cta_col:
        if st.button(
            f"Enrich {_just_n} new leads →",
            type="primary",
            key="enrich_new_leads",
        ):
            st.session_state["lead_ids_input"] = ", ".join(str(i) for i in _just_ids)
            st.session_state.pop("just_ingested_ids", None)
            st.session_state.pop("just_ingested_count", None)
            st.rerun()
    with dismiss_col:
        if st.button("Dismiss", key="dismiss_ingest_banner"):
            st.session_state.pop("just_ingested_ids", None)
            st.session_state.pop("just_ingested_count", None)
            st.rerun()

st.divider()

# =========================================================
# Section B — Run pipeline
# =========================================================
st.header("2) Run pipeline")

try:
    leads_df = list_leads()
except Exception as exc:
    st.error(f"Could not load leads: {exc}")
    leads_df = pd.DataFrame()

total_leads = len(leads_df)
st.caption(f"{total_leads} lead(s) currently in the database.")

if total_leads == 0:
    st.info("No leads to run. Ingest a CSV first.")
    st.stop()

if "_staged_ids" in st.session_state:
    st.session_state["lead_ids_input"] = st.session_state.pop("_staged_ids")

specific_ids_input = st.text_input(
    "Process specific lead IDs (optional, overrides count limit)",
    placeholder="e.g., 100 or 100, 102, 103 or 177-203",
    key="lead_ids_input",
    help="Comma-separated lead IDs or a range like 177-203. Leave empty to use the count limit below.",
)

try:
    _unenriched_ids = get_unenriched_lead_ids()
except Exception:
    _unenriched_ids = []
try:
    _unscored_ids = get_unscored_lead_ids()
except Exception:
    _unscored_ids = []
try:
    _content_pending_ids = get_content_pending_lead_ids()
except Exception:
    _content_pending_ids = []

qs1, qs2, qs3 = st.columns(3)
with qs1:
    if st.button(
        f"Set to unenriched ({len(_unenriched_ids)})",
        key="set_unenriched",
        help="Lead IDs with no enrichment row yet.",
        disabled=not _unenriched_ids,
    ):
        st.session_state["_staged_ids"] = ", ".join(str(i) for i in _unenriched_ids)
        st.rerun()
with qs2:
    if st.button(
        f"Set to unscored ({len(_unscored_ids)})",
        key="set_unscored",
        help="Leads that are enriched but have no Score row yet.",
        disabled=not _unscored_ids,
    ):
        st.session_state["_staged_ids"] = ", ".join(str(i) for i in _unscored_ids)
        st.rerun()
with qs3:
    if st.button(
        f"Set to content-pending ({len(_content_pending_ids)})",
        key="set_content_pending",
        help="Leads with a Score but no GeneratedContent row yet.",
        disabled=not _content_pending_ids,
    ):
        st.session_state["_staged_ids"] = ", ".join(str(i) for i in _content_pending_ids)
        st.rerun()

c1, c2 = st.columns(2)
with c1:
    dry_run = st.toggle(
        "Dry run (no real email sends)",
        value=True,
        key="run_dry",
        help="When ON, delivery only validates and logs — no API call is made to Instantly.",
    )
with c2:
    limit = st.number_input(
        "Lead count limit",
        min_value=1,
        max_value=int(total_leads),
        value=min(10, int(total_leads)),
        step=1,
        key="run_limit",
    )
    try:
        _enriched_n = int(kpi_counts().get("enriched", 0) or 0)
    except Exception:
        _enriched_n = 0
    if _enriched_n > 0 and _enriched_n <= total_leads:
        if st.button(
            f"Set to enriched-only count ({_enriched_n})",
            key="set_to_enriched_count",
            help="Limits this run to the leads that already have enrichment.",
        ):
            st.session_state["run_limit"] = _enriched_n
            st.rerun()

st.caption(
    "**Note**: this UI runs per-lead in a background thread (enrich → score → "
    "content → deliver, one lead at a time). The progress bar polls every 2s. "
    "Same DB writes as `scripts/run_pipeline.py`."
)

st.markdown("**Steps to run:**")
sc1, sc2, sc3, sc4, sc5 = st.columns(5)
with sc1:
    run_enrichment = st.checkbox("Enrich", value=True, key="run_enrich")
with sc2:
    run_scoring = st.checkbox("Score", value=True, key="run_score")
with sc3:
    run_email = st.checkbox(
        "Generate email",
        value=True,
        key="run_email",
        help="Also gates delivery — when unchecked, no email is generated or sent.",
    )
with sc4:
    run_call_script = st.checkbox(
        "Generate call script",
        value=False,
        key="run_call_script",
    )
with sc5:
    run_linkedin_msg = st.checkbox(
        "Generate LinkedIn DM",
        value=False,
        key="run_linkedin_msg",
    )

running = bool(st.session_state.get("pipeline_running"))

bcol1, bcol2 = st.columns([1, 1])
with bcol1:
    run_clicked = st.button(
        "Run pipeline",
        type="primary",
        disabled=running,
        key="run_btn",
    )
with bcol2:
    resume_clicked = st.button(
        "Resume failed leads",
        type="secondary",
        disabled=running,
        key="resume_btn",
        help=(
            "Re-runs scoring and content gen for leads missing those steps. "
            "Already-complete leads are skipped (no API calls)."
        ),
    )

# =========================================================
# Phase execution
# =========================================================
selected_ids: list[int] | None = None
if run_clicked:
    raw_ids = (specific_ids_input or "").strip()
    if raw_ids:
        # Accept comma-separated IDs or a range like '177-203'. Warn on (but
        # proceed past) IDs that don't exist in the DB.
        try:
            requested = parse_lead_ids(raw_ids)
        except ValueError as exc:
            st.error(
                f"{exc} Use comma-separated IDs (e.g. 177, 178, 179) or a "
                "range (e.g. 177-203)."
            )
            st.stop()
        existing = set(leads_df["id"].astype(int).tolist())
        valid: list[int] = []
        for lid in requested:
            if lid in existing:
                valid.append(lid)
            else:
                st.warning(f"Lead ID {lid} not found — skipped")
        if not valid:
            st.error("No valid lead IDs to process.")
            st.stop()
        # De-duplicate while preserving the user's order.
        seen_ids: set[int] = set()
        selected_ids = [
            lid for lid in valid if not (lid in seen_ids or seen_ids.add(lid))
        ]
    else:
        effective_limit = int(limit)
        if effective_limit > total_leads:
            st.warning(f"Clamped limit to {total_leads} (only that many leads exist).")
            effective_limit = total_leads
        selected_ids = leads_df.head(effective_limit)["id"].astype(int).tolist()
elif resume_clicked:
    all_lead_ids = leads_df["id"].astype(int).tolist()
    scored = get_scored_lead_ids(all_lead_ids)
    missing_score = [lid for lid in all_lead_ids if lid not in scored]
    eligible = _send_eligible_lead_ids(list(scored))
    has_email = get_content_lead_ids(eligible, "email")
    has_call = get_content_lead_ids(eligible, "call_script")
    has_li = get_content_lead_ids(eligible, "linkedin_msg")
    missing_content = [
        lid for lid in eligible
        if lid not in has_email or lid not in has_call or lid not in has_li
    ]
    resume_ids = sorted(set(missing_score) | set(missing_content))
    if not resume_ids:
        st.info("Nothing to resume — all leads complete.")
    else:
        st.info(f"Resuming {len(resume_ids)} incomplete lead(s).")
        selected_ids = resume_ids

if selected_ids and not running:
    # Kick off the background pipeline. We mutate a plain dict (held in
    # session_state) from the thread — never st.session_state itself,
    # because a thread spawned outside Streamlit has no ScriptRunContext.
    progress: dict = {
        "current": 0,
        "total": len(selected_ids),
        "current_lead": "",
        "phase": "starting",
        "done": False,
        "errors": [],
        "summary": None,
        "lead_ids": list(selected_ids),
    }
    st.session_state["pipeline_progress"] = progress
    st.session_state["pipeline_running"] = True

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(
            selected_ids,
            bool(dry_run),
            bool(run_enrichment),
            bool(run_scoring),
            bool(run_email),
            bool(run_call_script),
            bool(run_linkedin_msg),
            progress,
        ),
        daemon=True,
    )
    thread.start()
    st.rerun()

if running:
    prog = st.session_state.get("pipeline_progress", {})
    current = int(prog.get("current", 0))
    total = int(prog.get("total", 1)) or 1
    done = bool(prog.get("done", False))
    lead_id_str = str(prog.get("current_lead") or "")
    lead_ids_for_run = prog.get("lead_ids") or []

    st.write(
        f"Processing {total} lead(s) — IDs: `{lead_ids_for_run}`"
    )

    if not done:
        pct = current / total if total > 0 else 0.0
        st.progress(min(max(pct, 0.0), 1.0))
        caption_bits = [f"Processing lead {current} of {total}"]
        if lead_id_str:
            caption_bits.append(f"(ID {lead_id_str})")
        st.caption(" ".join(caption_bits))
        # Poll: yield briefly then rerun so the bar advances. The thread
        # mutates `prog` in place; the next run sees the latest counts.
        time.sleep(2)
        st.rerun()
    else:
        # Run finished — flip the gate so the buttons re-enable.
        st.session_state["pipeline_running"] = False
        st.cache_data.clear()

        errors = list(prog.get("errors") or [])
        summary = prog.get("summary") or {}

        if errors:
            st.warning(f"Completed with {len(errors)} error(s).")
            with st.expander("Errors", expanded=True):
                for e in errors:
                    st.write(f"- {e}")
        else:
            st.success(f"Done. Processed {total} lead(s).")

        if summary:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Enriched", summary.get("enriched", 0))
            m2.metric("Scored", summary.get("scored", 0))
            m3.metric("With email content", summary.get("content", 0))
            m4.metric("Delivered", summary.get("delivered", 0))

            tiers = summary.get("tiers") or {"A": 0, "B": 0, "C": 0}
            st.caption(
                f"Tier breakdown — A: {tiers.get('A', 0)}  ·  "
                f"B: {tiers.get('B', 0)}  ·  C: {tiers.get('C', 0)}"
            )

st.divider()

# =========================================================
# Section C — Bulk regenerate content by tier
# =========================================================
st.header("3) Bulk regenerate content")
st.caption(
    "Refresh existing content onto the current prompt version. Old rows are "
    "preserved as superseded versions; ratings and history are retained."
)

selected_tiers = st.multiselect(
    "Tiers to regenerate",
    options=["A", "B", "C"],
    default=["C"],
    key="bulk_regen_tiers",
    disabled=running,
)

try:
    candidate_ids = (
        get_leads_with_content_by_tier(selected_tiers) if selected_tiers else []
    )
except Exception as exc:
    st.error(f"Could not compute candidates: {exc}")
    candidate_ids = []

st.caption(
    f"{len(candidate_ids)} lead(s) with existing content in selected tier(s) "
    "will be regenerated. Old content is preserved as superseded versions; "
    "ratings and history are retained."
)

regen_pending = bool(st.session_state.get("bulk_regen_pending"))

if not regen_pending:
    if st.button(
        f"Regenerate content for {len(candidate_ids)} leads",
        type="primary",
        disabled=(len(candidate_ids) == 0 or running),
        key="bulk_regen_btn",
    ):
        st.session_state["bulk_regen_pending"] = True
        st.rerun()
    if selected_tiers and not candidate_ids:
        st.caption("No leads match the selected tiers.")
else:
    cc1, cc2 = st.columns([1, 1])
    confirm_regen = cc1.button(
        "Confirm regenerate", type="primary", key="bulk_regen_confirm"
    )
    cancel_regen = cc2.button(
        "Cancel", type="secondary", key="bulk_regen_cancel"
    )
    if cancel_regen:
        st.session_state["bulk_regen_pending"] = False
        st.rerun()
    if confirm_regen:
        st.session_state["bulk_regen_pending"] = False
        st.session_state["pipeline_running"] = True
        st.write(
            f"Regenerating {len(candidate_ids)} lead(s) — IDs: `{candidate_ids}`"
        )
        content_label = _PHASE_LABELS["content"]
        bulk_block = st.status(content_label, expanded=True, state="running")
        bulk_seen = {"n": 0}
        bulk_errs: list[dict] = []

        def bulk_on_update(u: PhaseUpdate) -> None:
            bulk_seen["n"] = u.idx
            bulk_block.update(
                label=f"{content_label}  ({u.idx}/{u.total})", state="running"
            )
            if u.ok and u.payload and u.payload.get("skipped"):
                reason = u.payload.get("reason") or "already complete"
                bulk_block.write(f"⏭ Lead {u.lead_id} — {reason}, skipping")
                return
            if u.ok:
                bulk_block.write(f"✅ Lead {u.lead_id}")
            else:
                bulk_block.write(f"❌ Lead {u.lead_id} — `{u.error}`")
                bulk_errs.append({"lead_id": u.lead_id, "error": u.error})

        try:
            bulk_regenerate_content(candidate_ids, on_update=bulk_on_update)
            bulk_block.update(
                label=f"{content_label}  ✓  ({bulk_seen['n']} processed)",
                state="complete",
                expanded=False,
            )
        except Exception as exc:
            st.error(f"Bulk regenerate failed: {exc}")
            bulk_block.update(state="error")
        else:
            st.success(
                f"Bulk regenerate complete. {bulk_seen['n']} lead(s) processed."
            )
            if bulk_errs:
                st.warning(f"{len(bulk_errs)} error(s) during regenerate:")
                st.dataframe(
                    pd.DataFrame(bulk_errs),
                    hide_index=True,
                    use_container_width=True,
                    key="bulk_regen_errors_table",
                )
        finally:
            st.session_state["pipeline_running"] = False
            st.cache_data.clear()
