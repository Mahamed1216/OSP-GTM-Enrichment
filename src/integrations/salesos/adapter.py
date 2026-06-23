"""SalesOS adapter — bridge shared Supabase rows ⇄ the existing engine pipeline.

This module orchestrates the EXISTING business logic; it does not reimplement
import/dedup, signal normalization, enrichment, scoring, or content generation.
It:

  1. Loads queued ``outbound_jobs`` and their SalesOS ``leads`` from shared Supabase.
  2. Converts a SalesOS lead record into the engine's internal lead shape, reusing
     the proven adapter + import path (so dedup, raw-payload storage, source-signal
     normalization, source-tier separation, and email_verified mapping all happen
     via existing code — source tier is stored SEPARATELY from the engine tier).
  3. Runs the option-gated pipeline (enrichment incl. buyer research, hiring
     signals, scoring, content) using the existing primitives.
  4. Writes enrichment/score/content back to the SalesOS-compatible contract tables.
  5. Preserves workspace/client isolation end to end.

It NEVER pushes to Instantly and NEVER sends email — sending is a separate,
approval-gated worker (``send_approved``).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

# Reuse the proven SalesOS-payload → ContactOut flattener + lead-id resolver so
# we don't duplicate that mapping logic.
from src.api.processing import _resolve_lead_id, salesos_lead_to_contact
from src.config import settings
from src.db import session_scope
from src.integrations.salesos import ensure_salesos_tables
from src.integrations.salesos.models import (
    CONTENT_PENDING_REVIEW,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SKIPPED,
    LeadEnrichment,
    LeadScore,
    OutboundContent,
    OutboundJob,
    SalesOSLead,
)
from src.models import Enrichment, GeneratedContent, Lead, Score, now_utc

log = logging.getLogger(__name__)

# Per-job content options. Email on by default; call script + LinkedIn off for
# cost control (the workspace ICP toggle is still the final cost guard — a
# generator returns None when its workspace toggle is disabled).
_DEFAULT_OPTIONS: dict[str, bool] = {
    "run_enrichment": True,
    "run_buyer_research": True,
    "run_hiring_signals": True,
    "run_scoring": True,
    "generate_email": True,
    "generate_call_script": False,
    "generate_linkedin": False,
}


def normalize_options(opts: Any) -> dict[str, bool]:
    out = dict(_DEFAULT_OPTIONS)
    if isinstance(opts, dict):
        for key in _DEFAULT_OPTIONS:
            if key in opts:
                out[key] = bool(opts[key])
    return out


# ---------------------------------------------------------------------------
# Job queue access + safe claiming
# ---------------------------------------------------------------------------

def list_queued_job_ids(
    *, limit: int = 10, workspace_id: int | None = None, client_id: str | None = None
) -> list[str]:
    """Return queued job ids (oldest-first), optionally scoped by tenant."""
    ensure_salesos_tables()
    with session_scope() as session:
        stmt = select(OutboundJob.id).where(OutboundJob.status == JOB_QUEUED)
        if workspace_id is not None:
            stmt = stmt.where(OutboundJob.workspace_id == workspace_id)
        if client_id is not None:
            stmt = stmt.where(OutboundJob.client_id == client_id)
        stmt = stmt.order_by(OutboundJob.requested_at.asc()).limit(limit)
        return list(session.execute(stmt).scalars().all())


def claim_job(job_id: str) -> bool:
    """Atomically claim a queued job (queued → running).

    Uses a conditional UPDATE guarded on ``status == queued`` so two workers
    racing for the same job can't both win — only the worker whose UPDATE
    actually changes a row (rowcount == 1) has claimed it.
    """
    with session_scope() as session:
        result = session.execute(
            update(OutboundJob)
            .where(OutboundJob.id == job_id, OutboundJob.status == JOB_QUEUED)
            .values(status=JOB_RUNNING, started_at=now_utc())
        )
        return result.rowcount == 1


def mark_job(job_id: str, status: str, *, error: str | None = None,
             engine_lead_id: int | None = None) -> None:
    with session_scope() as session:
        job = session.get(OutboundJob, job_id)
        if job is None:
            return
        job.status = status
        if error is not None:
            job.error = error[:4000]
        if engine_lead_id is not None:
            job.engine_lead_id = engine_lead_id
        if status in (JOB_COMPLETED, JOB_FAILED, JOB_SKIPPED):
            job.completed_at = now_utc()


# ---------------------------------------------------------------------------
# SalesOS lead → internal lead shape
# ---------------------------------------------------------------------------

def salesos_lead_to_nested(row: SalesOSLead) -> dict[str, Any]:
    """Convert a SalesOSLead ORM row into the nested lead dict the existing
    ``salesos_lead_to_contact`` adapter understands (person/company/signals)."""
    return {
        "external_contact_id": row.external_contact_id,
        "source": row.source or "salesos",
        "person": {
            "first_name": row.first_name,
            "last_name": row.last_name,
            "title": row.title,
            "email": row.email,
            "email_verified": bool(row.email_verified),
            "linkedin_url": row.linkedin_url,
        },
        "company": {
            "name": row.company_name,
            "domain": row.company_domain,
            "website": row.company_website,
            "industry": row.company_industry,
        },
        "source_signals": row.source_signals or [],
        "matched_icps": [],
        "source_tier": row.source_tier,
        "source_tier_score": row.source_tier_score,
        "raw_payload": row.raw_source_payload or {},
    }


def _import_salesos_lead(
    nested: dict, *, workspace_id: int | None, source: str
) -> int | None:
    """Import one SalesOS lead via the existing import path; return engine lead id.

    Reuses ``import_contacts`` so dedup, raw-payload storage, source-signal
    normalization, source-tier separation, and email_verified mapping all run
    through the proven code (no business logic duplicated here).
    """
    from src.lead_source.ingest import import_contacts, start_import_log

    contact = salesos_lead_to_contact(nested, source=source)
    import_id = start_import_log(
        workspace_id, source, requested_limit=1, base_url="", auto_run=True,
    )
    import_contacts(
        [contact], workspace_id=workspace_id, client_slug=source,
        import_id=import_id, external_source=source,
    )
    return _resolve_lead_id(workspace_id, contact, source)


# ---------------------------------------------------------------------------
# Option-gated pipeline (reuses existing primitives)
# ---------------------------------------------------------------------------

def _has_enrichment(lead_id: int) -> bool:
    with session_scope() as session:
        return session.execute(
            select(Enrichment.id).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none() is not None


async def _run_pipeline(lead_id: int, workspace_id: int | None, options: dict) -> None:
    """Run the requested steps for one engine lead. Reuses existing primitives."""
    if options["run_enrichment"] or options["run_buyer_research"]:
        if not _has_enrichment(lead_id):
            from src.enrichment.waterfall import enrich_lead
            await enrich_lead(lead_id, workspace_id=workspace_id)

    if options["run_hiring_signals"]:
        from src.signals.hiring import enrich_hiring_signal
        await enrich_hiring_signal(lead_id, workspace_id=workspace_id)

    if options["run_scoring"]:
        from src.scoring import score_lead
        await score_lead(lead_id, workspace_id=workspace_id)

    # Content — email by default; call script / LinkedIn only when the job asks
    # AND the workspace toggle allows it (the generator returns None otherwise).
    if options["generate_email"]:
        from src.content.email import generate_email
        await generate_email(lead_id, workspace_id=workspace_id)
    if options["generate_call_script"]:
        from src.content.call_script import generate_call_script
        await generate_call_script(lead_id, workspace_id=workspace_id)
    if options["generate_linkedin"]:
        from src.content.linkedin_msg import generate_linkedin_msg
        await generate_linkedin_msg(lead_id, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Write results back to the SalesOS contract tables
# ---------------------------------------------------------------------------

def _latest_content(session: Session, lead_id: int, kind: str) -> GeneratedContent | None:
    return session.execute(
        select(GeneratedContent)
        .where(GeneratedContent.lead_id == lead_id, GeneratedContent.kind == kind)
        .order_by(GeneratedContent.id.desc())
    ).scalars().first()


def mirror_results(salesos_lead_id: str, engine_lead_id: int) -> dict[str, bool]:
    """Mirror engine enrichment/score/content into the SalesOS contract tables.

    Idempotent: re-running updates the existing enrichment/score rows and
    appends the freshest content snapshot. Returns a small status map.
    """
    from src.delivery.eligibility import is_unsafe_internal_content

    wrote = {"enrichment": False, "score": False, "content": False}
    with session_scope() as session:
        enr = session.execute(
            select(Enrichment).where(Enrichment.lead_id == engine_lead_id)
        ).scalar_one_or_none()
        score = session.execute(
            select(Score).where(Score.lead_id == engine_lead_id)
        ).scalar_one_or_none()
        email = _latest_content(session, engine_lead_id, "email")
        call = _latest_content(session, engine_lead_id, "call_script")
        linkedin = _latest_content(session, engine_lead_id, "linkedin_msg")

        # --- enrichment ---
        if enr is not None:
            ba = enr.buyer_accounts or {}
            row = session.execute(
                select(LeadEnrichment).where(LeadEnrichment.lead_id == salesos_lead_id)
            ).scalar_one_or_none()
            values = dict(
                linkedin_profile=enr.linkedin_profile,
                company_details=enr.company_details,
                company_news=enr.company_news,
                industry_news=enr.industry_news,
                buyer_account_research=ba,
                tavily_metadata=ba.get("research_metadata") if isinstance(ba, dict) else None,
                source_status=enr.source_status,
                updated_at=now_utc(),
            )
            if row is None:
                session.add(LeadEnrichment(lead_id=salesos_lead_id, **values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            wrote["enrichment"] = True

        # --- score (engine tier — NEVER the source tier) ---
        if score is not None:
            row = session.execute(
                select(LeadScore).where(LeadScore.lead_id == salesos_lead_id)
            ).scalar_one_or_none()
            values = dict(
                score=int(score.score) if score.score is not None else None,
                tier=score.tier,
                rationale=score.rationale,
                signals_used=list(score.signals_used or []),
                model_version=score.model,
                scored_at=now_utc(),
            )
            if row is None:
                session.add(LeadScore(lead_id=salesos_lead_id, **values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            wrote["score"] = True

        # --- content (awaiting CSM review) ---
        if email is not None or call is not None or linkedin is not None:
            subj = email.subject if email else None
            body = email.body if email else None
            unsafe = is_unsafe_internal_content(subj, body)
            missing = not (body or "").strip()
            safety_status = "needs_review" if (unsafe or missing) else "ok"
            blocked = None
            if missing:
                blocked = "no_content"
            elif unsafe:
                blocked = "unsafe_content"
            session.add(OutboundContent(
                lead_id=salesos_lead_id,
                email_subject=subj,
                email_body=body,
                call_script=(call.body if call else None),
                linkedin_message=(linkedin.body if linkedin else None),
                content_status=CONTENT_PENDING_REVIEW,
                safety_status=safety_status,
                blocked_reason=blocked,
                prompt_version=(email.prompt_version if email else None),
                model_version=(email.model if email else settings.content_model),
                engine_content_id=(email.id if email else None),
            ))
            wrote["content"] = True

    return wrote


# ---------------------------------------------------------------------------
# Top-level: process one job
# ---------------------------------------------------------------------------

def process_job(job_id: str) -> dict[str, Any]:
    """Run a single (already-claimed, status=running) outbound job to completion.

    Imports the SalesOS lead, runs the option-gated pipeline, mirrors results
    back, and marks the job completed/failed/skipped. Never raises — failures
    are captured on the job row (status=failed, error set).
    """
    import asyncio

    ensure_salesos_tables()
    with session_scope() as session:
        job = session.get(OutboundJob, job_id)
        if job is None:
            return {"job_id": job_id, "status": "failed", "error": "job_not_found"}
        lead_row = session.get(SalesOSLead, job.lead_id)
        if lead_row is None:
            workspace_id = job.workspace_id
            source = "salesos"
            nested = None
        else:
            workspace_id = job.workspace_id if job.workspace_id is not None else lead_row.workspace_id
            source = lead_row.source or "salesos"
            nested = salesos_lead_to_nested(lead_row)
        salesos_lead_id = job.lead_id
        options = normalize_options(job.options)

    if nested is None:
        mark_job(job_id, JOB_FAILED, error="salesos_lead_not_found")
        return {"job_id": job_id, "status": JOB_FAILED, "error": "salesos_lead_not_found"}

    try:
        engine_lead_id = _import_salesos_lead(
            nested, workspace_id=workspace_id, source=source
        )
        if engine_lead_id is None:
            mark_job(job_id, JOB_SKIPPED, error="no_identity_or_not_imported")
            return {"job_id": job_id, "status": JOB_SKIPPED,
                    "error": "no_identity_or_not_imported"}

        asyncio.run(_run_pipeline(engine_lead_id, workspace_id, options))
        wrote = mirror_results(salesos_lead_id, engine_lead_id)
        mark_job(job_id, JOB_COMPLETED, engine_lead_id=engine_lead_id)
        log.info("salesos_job_completed",
                 extra={"job_id": job_id, "engine_lead_id": engine_lead_id, "wrote": wrote})
        return {"job_id": job_id, "status": JOB_COMPLETED,
                "engine_lead_id": engine_lead_id, "wrote": wrote}
    except Exception as exc:  # captured on the job — never crashes the batch
        err = f"{type(exc).__name__}: {exc}"
        mark_job(job_id, JOB_FAILED, error=err)
        log.warning("salesos_job_failed", extra={"job_id": job_id, "error": err})
        return {"job_id": job_id, "status": JOB_FAILED, "error": err}


def claim_and_process(job_id: str) -> dict[str, Any]:
    """Safely claim a queued job, then process it. Skips if already claimed."""
    if not claim_job(job_id):
        return {"job_id": job_id, "status": "skipped", "error": "not_claimable"}
    return process_job(job_id)
