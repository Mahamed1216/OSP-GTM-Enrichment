"""Internal-API processing: adapt SalesOS payloads → existing pipeline.

This module orchestrates the EXISTING business logic — it does not reimplement
import/dedup, signal normalization, enrichment, scoring, or content. It:

  1. Adapts the nested SalesOS lead shape into the flat ContactOut shape that
     src.lead_source.ingest.import_contacts already understands (so dedup, raw
     payload storage, source-signal normalization, source-tier separation, and
     email_verified mapping all happen via the existing code path).
  2. Runs the option-gated steps (enrichment incl. buyer research, hiring
     signals, scoring, email) using the existing primitives.
  3. Builds the processed-lead payload SalesOS expects (research/score/content/
     safety) for the connector layer to route.

Hard guarantees: NEVER pushes to Instantly, NEVER sends email, NEVER generates
call scripts or LinkedIn DMs (only email, and only when generate_email=true).
Workspace scoping is preserved end-to-end.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from src.api.run_store import complete_run, get_run, mark_running
from src.config import settings
from src.db import session_scope
from src.models import Enrichment, GeneratedContent, Lead, LeadSignal, Score, now_utc

log = logging.getLogger(__name__)

API_VERSION = "v1"

# Conservative defaults. Email is the only content type ever generated here;
# call scripts / LinkedIn DMs are never produced by the API. push_to_instantly
# is always False at this layer (rejected upstream in the server).
_DEFAULT_OPTIONS: dict[str, bool] = {
    "run_enrichment": True,
    "run_buyer_research": True,
    "run_hiring_signals": True,
    "run_scoring": True,
    "generate_email": True,
    "push_to_instantly": False,
}


def normalize_options(opts: Any) -> dict[str, bool]:
    out = dict(_DEFAULT_OPTIONS)
    if isinstance(opts, dict):
        for key in _DEFAULT_OPTIONS:
            if key in opts:
                out[key] = bool(opts[key])
    out["push_to_instantly"] = False  # never honored at this layer
    return out


# ---------------------------------------------------------------------------
# Adapter: SalesOS nested lead → flat ContactOut dict
# ---------------------------------------------------------------------------

def salesos_lead_to_contact(lead: dict, *, source: str = "salesos") -> dict:
    """Flatten one SalesOS lead into the ContactOut shape import_contacts expects.

    The full original SalesOS payload is preserved under ``salesos_payload`` so
    the raw source payload is stored verbatim (in addition to the flattened
    fields used for mapping/dedup).
    """
    lead = lead or {}
    person = lead.get("person") or {}
    company = lead.get("company") or {}
    raw = lead.get("raw_payload") if isinstance(lead.get("raw_payload"), dict) else {}

    contact: dict[str, Any] = dict(raw)  # start from the source raw_payload
    contact.update({
        "id": lead.get("external_contact_id") or person.get("id") or contact.get("id"),
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name"),
        "title": person.get("title"),
        "email": person.get("email"),
        "email_verified": bool(person.get("email_verified", False)),
        "linkedin_url": person.get("linkedin_url"),
        "mobile_phone": person.get("phone") or person.get("mobile_phone"),
        "company_name": company.get("name"),
        "company_domain": company.get("domain"),
        "company_industry": company.get("industry"),
        "company_size": company.get("employee_count") or company.get("company_size"),
        # Signal-first payload — parse_source_signals normalizes these.
        "signals": lead.get("source_signals") or [],
        "signal_count": lead.get("signal_count"),
        "matched_icps": lead.get("matched_icps") or [],
        "tier": lead.get("source_tier"),
        "tier_score": lead.get("source_tier_score"),
        "source": contact.get("source") or source,
        # Verbatim original for provenance/audit.
        "salesos_payload": lead,
    })
    return contact


def _resolve_lead_id(workspace_id: int | None, contact: dict, source: str) -> int | None:
    """Resolve the lead id a contact maps to, reusing the existing dedup priority."""
    from src.lead_source.ingest import _find_existing
    from src.lead_source.mapper import map_contact

    fields = map_contact(contact)
    fields["external_source"] = source
    with session_scope() as session:
        row = _find_existing(session, fields, workspace_id)
        return row.id if row is not None else None


# ---------------------------------------------------------------------------
# Per-lead option-gated processing (reuses existing primitives)
# ---------------------------------------------------------------------------

def _has_enrichment(lead_id: int) -> bool:
    with session_scope() as session:
        return session.execute(
            select(Enrichment.id).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none() is not None


async def _process_one_lead(lead_id: int, workspace_id: int | None, options: dict) -> dict:
    """Run the requested steps for one lead. Returns a per-step status map."""
    steps: dict[str, str] = {}

    # Enrichment + buyer research (buyer research runs inside the enrich_lead
    # waterfall, so run_buyer_research implies enrichment). Idempotent.
    if options["run_enrichment"] or options["run_buyer_research"]:
        if _has_enrichment(lead_id):
            steps["enrichment"] = "skipped_exists"
        else:
            from src.enrichment.waterfall import enrich_lead
            await enrich_lead(lead_id, workspace_id=workspace_id)
            steps["enrichment"] = "done"

    # Hiring signals (C-tier rescue). Idempotent + graceful.
    if options["run_hiring_signals"]:
        from src.signals.hiring import enrich_hiring_signal
        await enrich_hiring_signal(lead_id, workspace_id=workspace_id)
        steps["hiring_signals"] = "done"

    # Scoring (applies source/hiring signal uplifts internally).
    if options["run_scoring"]:
        from src.scoring import score_lead
        await score_lead(lead_id, workspace_id=workspace_id)
        steps["scoring"] = "done"

    # Email only — never call scripts / LinkedIn DMs from the API.
    if options["generate_email"]:
        from src.content.email import generate_email
        await generate_email(lead_id, workspace_id=workspace_id)
        steps["email"] = "done"

    return steps


# ---------------------------------------------------------------------------
# Processed-lead payload builder
# ---------------------------------------------------------------------------

def _first_segment(ba: dict) -> str | None:
    for key in ("likely_buyer_segments", "trigger_based_buyer_segments",
                "likely_direct_buyers", "likely_buyer_accounts"):
        v = ba.get(key)
        if isinstance(v, list) and v:
            return str(v[0])
    return None


def _strongest_signal(ba: dict) -> str | None:
    sig = ba.get("selected_signal")
    if isinstance(sig, dict):
        return sig.get("summary") or sig.get("signal") or sig.get("name") or sig.get("title")
    if isinstance(sig, str):
        return sig
    return None


def _ba_urls(ba: dict) -> list[str]:
    urls: list[str] = []
    sig = ba.get("selected_signal")
    if isinstance(sig, dict):
        u = sig.get("source_url") or sig.get("url")
        if u:
            urls.append(str(u))
    for c in (ba.get("candidate_signals") or []):
        if isinstance(c, dict):
            u = c.get("source_url") or c.get("url")
            if u:
                urls.append(str(u))
    # de-dup, cap
    seen: list[str] = []
    for u in urls:
        if u not in seen:
            seen.append(u)
    return seen[:10]


def _buyer_summary(ba: dict) -> str | None:
    parts: list[str] = []
    if ba.get("buyer_motion"):
        parts.append(f"motion={ba['buyer_motion']}")
    seg = _first_segment(ba)
    if seg:
        parts.append(seg)
    return "; ".join(parts) or None


def build_processed_payload(lead_id: int, workspace_id: int | None) -> dict:
    """Assemble the SalesOS-facing processed payload for one lead."""
    from src.delivery.eligibility import (
        _is_verified,
        filter_eligible,
        is_unsafe_internal_content,
    )

    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return {}
        score = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        content = session.execute(
            select(GeneratedContent)
            .where(GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "email")
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()
        enrichment = session.execute(
            select(Enrichment).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none()
        src_sig = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == "source_import",
            )
        ).scalar_one_or_none()

        # Snapshot everything inside the session.
        person = {
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "title": lead.title,
            "email": lead.email,
            "email_verified": _is_verified(lead),
            "linkedin_url": lead.linkedin_url,
        }
        company = {
            "name": lead.company,
            "domain": lead.company_domain,
            "industry": lead.industry,
        }
        external_contact_id = lead.external_contact_id
        source = lead.external_source
        source_tier = lead.source_tier
        source_tier_score = lead.source_tier_score
        ba = (enrichment.buyer_accounts or {}) if enrichment else {}
        subj = content.subject if content else None
        body = content.body if content else None
        content_prompt_version = content.prompt_version if content else None
        content_model = content.model if content else None

        src_payload = (src_sig.raw_payload or {}) if src_sig is not None else {}
        src_angle = src_sig.recommended_email_angle if src_sig is not None else None

        score_dict = None
        if score is not None:
            score_dict = {
                "score": int(score.score),
                "tier": score.tier,
                "rationale": score.rationale,
                "signals_used": list(score.signals_used or []),
            }

        # Safety (reuses the bulk-push eligibility filter — same gates).
        eligible, skipped = filter_eligible([lead_id], session)
        eligible_for_push = lead_id in eligible
        blocked_reason = None
        for code, ids in skipped.items():
            if lead_id in ids:
                blocked_reason = code
                break
        unsafe = is_unsafe_internal_content(subj, body)
        missing_content = not (body or "").strip()
        email_verified = person["email_verified"]

    # Normalized source signals (Dele shape) from the stored parsed payload.
    source_signals = [
        {
            "type": s.get("signal_type"),
            "value": s.get("signal_value"),
            "confidence": s.get("signal_confidence"),
            "strength": s.get("signal_strength"),
            "source": s.get("signal_source"),
            "source_url": s.get("signal_source_url"),
            "detected_at": s.get("signal_detected_at"),
        }
        for s in (src_payload.get("signals") or [])
    ]

    if missing_content:
        content_status = "missing"
    elif unsafe:
        content_status = "needs_review"
    else:
        content_status = "ready"

    if eligible_for_push:
        action = "ready_for_sequence"
    elif missing_content or unsafe:
        action = "needs_content_review"
    else:
        action = "needs_review"

    workspace_slug = _workspace_slug(workspace_id)

    return {
        "workspace_id": workspace_slug or workspace_id,
        "lead_id": lead_id,
        "external_contact_id": external_contact_id,
        "source": source,
        "person": person,
        "company": company,
        "source_signals": source_signals,
        "source_tier": source_tier,
        "source_tier_score": source_tier_score,
        "research": {
            "buyer_segment": _first_segment(ba),
            "buyer_research_summary": _buyer_summary(ba),
            "strongest_signal": _strongest_signal(ba),
            "signal_source_urls": _ba_urls(ba),
            "research_confidence": ba.get("buyer_research_confidence"),
        },
        "score": score_dict,
        "generated_content": {
            "email_subject": subj,
            "email_body": body,
            "personalization_angle": src_angle,
            "content_status": content_status,
        },
        "safety": {
            "eligible_for_push": eligible_for_push,
            "blocked_reason": blocked_reason,
            "email_verified": email_verified,
            "unsafe_internal_content_detected": unsafe,
            "missing_content": missing_content,
        },
        "recommended_action": {
            "action": action,
            "destination_options": ["SalesOS_connector_layer"],
        },
        "metadata": {
            "processed_at": now_utc().isoformat(),
            "model_version": content_model or settings.content_model,
            "prompt_version": content_prompt_version,
            "api_version": API_VERSION,
        },
    }


def _workspace_slug(workspace_id: int | None) -> str | None:
    if workspace_id is None:
        return None
    try:
        from src.workspace import get_workspace_by_id
        ws = get_workspace_by_id(workspace_id)
        return (ws or {}).get("slug")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Run orchestration (sync import + async option-gated processing)
# ---------------------------------------------------------------------------

async def process_run(run_id: str) -> dict:
    """Process all leads in an api_runs record. Updates the run; returns a summary.

    Import (dedup, raw storage, signal normalization, source-tier separation,
    email_verified mapping) happens once via the existing import_contacts. Then
    each resolved lead runs the option-gated steps. Never raises out — failures
    are captured per lead and on the run record.
    """
    from src.lead_source.ingest import import_contacts, start_import_log

    run = get_run(run_id)
    if run is None:
        return {"status": "failed", "error": "run_not_found"}

    mark_running(run_id)
    req = run.get("request_payload") or {}
    workspace_id = run.get("workspace_id")
    source = run.get("source") or "salesos"
    options = normalize_options(req.get("options"))
    leads = req.get("leads") or []

    results: list[dict] = []
    processed = 0
    failed = 0

    try:
        contacts = [salesos_lead_to_contact(l, source=source) for l in leads]
        import_id = start_import_log(
            workspace_id, source, requested_limit=len(contacts),
            base_url="", auto_run=True,
        )
        # Reuse the full import path: dedup, raw payload, signal normalization,
        # source tier (stored separately), email_verified mapping.
        import_contacts(
            contacts,
            workspace_id=workspace_id,
            client_slug=source,
            import_id=import_id,
            external_source=source,
        )

        for original, contact in zip(leads, contacts):
            ext_id = contact.get("id")
            try:
                lead_id = _resolve_lead_id(workspace_id, contact, source)
                if lead_id is None:
                    failed += 1
                    results.append({
                        "external_contact_id": ext_id, "lead_id": None,
                        "status": "skipped", "error": "no_identity_or_not_imported",
                    })
                    continue
                await _process_one_lead(lead_id, workspace_id, options)
                payload = build_processed_payload(lead_id, workspace_id)
                results.append({
                    "external_contact_id": ext_id, "lead_id": lead_id,
                    "status": "processed", "processed": payload,
                })
                processed += 1
            except Exception as exc:
                failed += 1
                results.append({
                    "external_contact_id": ext_id, "lead_id": None,
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                })
                log.warning("api_process_lead_failed",
                            extra={"run_id": run_id, "external_contact_id": ext_id,
                                   "error": f"{type(exc).__name__}: {exc}"})

        if failed == 0:
            status = "completed"
        elif processed > 0:
            status = "partial"
        else:
            status = "failed"

        complete_run(
            run_id, status=status, result_payload={"results": results},
            processed_count=processed, failed_count=failed,
        )
        return {"status": status, "results": results,
                "processed_count": processed, "failed_count": failed}

    except Exception as exc:
        complete_run(
            run_id, status="failed", error=f"{type(exc).__name__}: {exc}",
            result_payload={"results": results},
            processed_count=processed, failed_count=failed,
        )
        log.warning("api_process_run_failed",
                    extra={"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                "results": results}
