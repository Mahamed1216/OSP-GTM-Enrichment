"""Run Pipeline — CSV ingest + four-phase pipeline run with live st.status."""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.lib.db_queries import (
    get_content_lead_ids,
    get_leads_with_content_by_tier,
    get_scored_lead_ids,
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
    bulk_regenerate_content,
    run_phased_pipeline,
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


st.title("Run Pipeline")

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
        else:
            st.error(f"Ingest subprocess exited with code {result.returncode}")

        with st.expander("Subprocess output", expanded=not result.ok):
            if result.stdout:
                st.code(result.stdout, language="text")
            if result.stderr:
                st.code(result.stderr, language="text")

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

specific_ids_input = st.text_input(
    "Process specific lead IDs (optional, overrides count limit)",
    placeholder="e.g., 100 or 100, 102, 103",
    key="run_specific_ids",
    help="Comma-separated lead IDs. Leave empty to use the count limit below.",
)

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
    "**Note**: this UI runs phases across the full batch (enrich-all → score-all → "
    "content-all → deliver-all). For per-lead end-to-end traversal, use "
    "`scripts/run_pipeline.py`. Same DB writes either way."
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
        # Parse the comma-separated list. Bail on any non-integer token; warn
        # on (but proceed past) IDs that don't exist in the DB.
        tokens = [t.strip() for t in raw_ids.split(",") if t.strip()]
        try:
            requested = [int(t) for t in tokens]
        except ValueError:
            st.error(
                "Invalid lead ID format: must be integers separated by commas"
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

if selected_ids:
    st.session_state["pipeline_running"] = True
    st.write(f"Processing {len(selected_ids)} lead(s) — IDs: `{selected_ids}`")

    # One status block per phase. We pre-open them so the user sees the full
    # roadmap, and update each as the corresponding phase fires.
    status_blocks = {
        phase: st.status(label, expanded=True, state="running" if i == 0 else "complete")
        for i, (phase, label) in enumerate(_PHASE_LABELS.items())
    }
    # All non-current phases start collapsed/idle. Reopen properly.
    for phase, block in status_blocks.items():
        block.update(state="running" if phase == "enrichment" else "complete")

    # Stats accumulators per phase
    seen: dict[str, int] = {p: 0 for p in _PHASE_LABELS}
    errs: list[dict] = []

    def on_update(u: PhaseUpdate) -> None:
        block = status_blocks[u.phase]
        seen[u.phase] = u.idx
        new_label = f"{_PHASE_LABELS[u.phase]}  ({u.idx}/{u.total})"
        block.update(label=new_label, state="running")
        if u.ok and u.payload and u.payload.get("skipped"):
            reason = u.payload.get("reason") or "already complete"
            block.write(f"⏭ Lead {u.lead_id} — {reason}, skipping")
            return
        if u.ok:
            extra = ""
            if u.phase == "scoring" and u.payload:
                extra = f" — score {u.payload.get('score')} ({u.payload.get('tier')})"
            elif u.phase == "delivery" and u.payload:
                if u.payload.get("delivered"):
                    extra = " — delivered" + (" (dry-run)" if u.payload.get("dry_run") else "")
                else:
                    reason = u.payload.get("skip_reason") or "skipped"
                    extra = f" — skipped: {reason}"
            block.write(f"✅ Lead {u.lead_id}{extra}")
        else:
            block.write(f"❌ Lead {u.lead_id} — `{u.error}`")
            errs.append({"phase": u.phase, "lead_id": u.lead_id, "error": u.error})

    try:
        summary = run_phased_pipeline(
            selected_ids,
            dry_run=bool(dry_run),
            on_update=on_update,
        )
        # Mark each phase complete
        for phase, block in status_blocks.items():
            count = seen[phase]
            block.update(
                label=f"{_PHASE_LABELS[phase]}  ✓  ({count} processed)",
                state="complete",
                expanded=False,
            )
    except Exception as exc:
        st.error(f"Pipeline failed: {exc}")
        for block in status_blocks.values():
            block.update(state="error")
    else:
        st.success("Pipeline run complete.")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Enriched", summary["enriched"])
        m2.metric("Scored", summary["scored"])
        m3.metric("With email content", summary["content"])
        m4.metric("Delivered", summary["delivered"])

        tiers = summary["tiers"]
        st.caption(
            f"Tier breakdown — A: {tiers['A']}  ·  B: {tiers['B']}  ·  C: {tiers['C']}"
        )

        if errs:
            st.warning(f"{len(errs)} error(s) during the run:")
            st.dataframe(
                pd.DataFrame(errs),
                hide_index=True,
                use_container_width=True,
                key="run_errors_table",
            )
    finally:
        st.session_state["pipeline_running"] = False
        st.cache_data.clear()

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
