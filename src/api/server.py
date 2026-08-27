"""Internal API for the operator console and external callers.

A caller POSTs lead payloads here; we run the pipeline (enrichment, buyer
research, signal capture, scoring, email, safety) and return the processed
payload. We NEVER push to Instantly and NEVER send email from this API.

Run:
    uvicorn src.api.server:app --host 0.0.0.0 --port 8000

Auth: every /api/v1 endpoint requires ``Authorization: Bearer <INTERNAL_API_KEY>``.
/health is public. The full key is never logged.
"""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.api import run_store
from src.api.auth import (
    COOKIE_NAME,
    AuthNotConfigured,
    admin_password,
    cookie_kwargs,
    issue_token,
    password_matches,
    verify_token,
)
from src.api.processing import build_processed_payload, normalize_options, process_run

log = logging.getLogger(__name__)

API_VERSION = "v1"
SERVICE_NAME = "osp-gtm-enrichment"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Ensure the schema exists when running as a long-lived server. Guarded +
    # idempotent; never fatal at boot. The Vercel function does not run this.
    try:
        from src.db import init_db
        init_db()
    except Exception as exc:  # pragma: no cover - defensive boot guard
        log.warning("api_init_db_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
    yield


app = FastAPI(
    title="OSP GTM Enrichment — Internal API",
    description="API to process sourced leads through the enrichment pipeline.",
    version=API_VERSION,
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Auth — Bearer INTERNAL_API_KEY
# ---------------------------------------------------------------------------

def _expected_api_key() -> str:
    return os.environ.get("INTERNAL_API_KEY", "")


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _internal_key_ok(authorization: str | None) -> bool:
    """Backend-to-backend auth: Authorization: Bearer <INTERNAL_API_KEY>."""
    expected = _expected_api_key()
    provided = _bearer_token(authorization)
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


async def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Allow an admin console session, or an internal bearer key.

    The console signs in with ADMIN_PASSWORD and carries an HttpOnly cookie —
    the browser never handles INTERNAL_API_KEY. The bearer path stays for
    backend-to-backend callers (cron, scripts, other services).
    """
    if verify_token(request.cookies.get(COOKIE_NAME)):
        return
    if _internal_key_ok(authorization):
        return

    if not admin_password() and not _expected_api_key():
        log.error("admin_auth_not_configured")
        raise HTTPException(
            status_code=500,
            detail=(
                "Neither ADMIN_PASSWORD nor INTERNAL_API_KEY is configured on "
                "this server."
            ),
        )
    # Log presence only — never a credential value.
    log.warning("api_auth_failed", extra={"bearer_present": bool(authorization)})
    raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# Kept so existing internal callers and tests that import it keep working.
require_api_key = require_admin


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProcessRequest(BaseModel):
    workspace_slug: str | None = None
    workspace_id: int | None = None
    source: str | None = "api"
    run_mode: str = "async"  # "async" (default) | "sync"
    options: dict | None = None
    leads: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Health (public)
# ---------------------------------------------------------------------------

@app.get("/health", summary="Health check (public)")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": API_VERSION}


# ---------------------------------------------------------------------------
# Process lead/batch
# ---------------------------------------------------------------------------

def _resolve_workspace_id(req: ProcessRequest) -> int | None:
    from src.workspace import get_workspace_by_id, get_workspace_by_slug
    if req.workspace_id is not None:
        ws = get_workspace_by_id(req.workspace_id)
        return ws["id"] if ws else None
    if req.workspace_slug:
        ws = get_workspace_by_slug(req.workspace_slug)
        return ws["id"] if ws else None
    return None


@app.post(
    "/api/v1/leads/process",
    summary="Process one lead or a batch",
    dependencies=[Depends(require_api_key)],
)
async def process_leads(req: ProcessRequest) -> dict:
    # Instantly push is never supported via the API.
    opts = req.options or {}
    if opts.get("push_to_instantly") is True:
        raise HTTPException(status_code=400, detail="instant_push_not_supported_via_api")

    if not req.leads:
        raise HTTPException(status_code=422, detail="leads must be a non-empty list.")

    workspace_id = _resolve_workspace_id(req)
    if workspace_id is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown workspace (provide a valid workspace_slug or workspace_id).",
        )

    run_mode = "sync" if (req.run_mode or "").lower() == "sync" else "async"
    # Persist normalized options (push_to_instantly forced False).
    request_payload = {
        "source": req.source,
        "options": normalize_options(req.options),
        "leads": req.leads,
    }
    run_id = run_store.create_run(
        workspace_id=workspace_id,
        source=req.source,
        run_mode=run_mode,
        request_payload=request_payload,
        lead_count=len(req.leads),
        status=("running" if run_mode == "sync" else "queued"),
    )

    if run_mode == "sync":
        summary = await process_run(run_id)
        run = run_store.get_run(run_id) or {}
        return {
            "run_id": run_id,
            "status": run.get("status", summary.get("status")),
            "lead_count": len(req.leads),
            "results": [
                r.get("processed")
                for r in (summary.get("results") or [])
                if r.get("processed")
            ],
        }

    # Async: a worker (python -m src.api.worker) will process the queued run.
    return {"run_id": run_id, "status": "queued", "lead_count": len(req.leads)}


# ---------------------------------------------------------------------------
# Run status
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/runs/{run_id}",
    summary="Get the status of a process run",
    dependencies=[Depends(require_api_key)],
)
async def get_run_status(run_id: str) -> dict:
    run = run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")

    result_payload = run.get("result_payload") or {}
    out = {
        "run_id": run["run_id"],
        "status": run["status"],
        "created_at": _iso(run.get("created_at")),
        "updated_at": _iso(run.get("updated_at")),
        "completed_at": _iso(run.get("completed_at")),
        "lead_count": run.get("lead_count", 0),
        "processed_count": run.get("processed_count", 0),
        "failed_count": run.get("failed_count", 0),
        "results": result_payload.get("results", []),
        "error": run.get("error"),
    }
    return out


# ---------------------------------------------------------------------------
# Processed lead
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/leads/{lead_id}/processed",
    summary="Get the processed payload for a lead",
    dependencies=[Depends(require_api_key)],
)
async def get_processed_lead(
    lead_id: int,
    workspace_slug: str | None = None,
    workspace_id: int | None = None,
) -> dict:
    from src.db import session_scope
    from src.models import Lead

    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        lead_ws = lead.workspace_id if lead is not None else None
        exists = lead is not None
    if not exists:
        raise HTTPException(status_code=404, detail="lead_not_found")

    # Workspace scoping: if a workspace filter is supplied, the lead must belong
    # to it (prevents cross-workspace reads via the API).
    requested_ws = workspace_id
    if requested_ws is None and workspace_slug:
        from src.workspace import get_workspace_by_slug
        ws = get_workspace_by_slug(workspace_slug)
        requested_ws = ws["id"] if ws else -1  # force mismatch on unknown slug
    if requested_ws is not None and requested_ws != lead_ws:
        raise HTTPException(status_code=404, detail="lead_not_found_in_workspace")

    return build_processed_payload(lead_id, lead_ws)


def _iso(value) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


# ---------------------------------------------------------------------------
# Console read APIs
#
# Everything below backs the operator console. All are read-only, workspace
# scoped, and reuse the query layer in src/lib/db_queries.py rather than
# introducing a second set of SQL. None of them return a secret value.
# ---------------------------------------------------------------------------

def _full_name(lead) -> str:
    return f"{lead.first_name or ''} {lead.last_name or ''}".strip()


def _default_ws(workspace_id: int | None, workspace_slug: str | None) -> int | None:
    """Resolve the workspace to read, falling back to the default one."""
    if workspace_id is not None:
        return workspace_id
    if workspace_slug:
        from src.workspace import get_workspace_by_slug
        ws = get_workspace_by_slug(workspace_slug)
        return ws["id"] if ws else -1  # unknown slug -> match nothing
    from src.workspace import get_default_workspace_id
    try:
        return get_default_workspace_id()
    except Exception:
        return None


@app.get(
    "/api/v1/dashboard/summary",
    summary="Counts, tier split and recent activity for the console home",
    dependencies=[Depends(require_api_key)],
)
async def dashboard_summary(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
) -> dict:
    from src.lib.db_queries import (
        kpi_counts,
        ready_to_send_count,
        recent_activity,
        tier_distribution,
    )

    ws = _default_ws(workspace_id, workspace_slug)
    counts = kpi_counts(ws)
    recent_runs = run_store.list_recent_runs(limit=5, workspace_id=ws)
    return {
        "workspace_id": ws,
        "counts": counts,
        "tiers": tier_distribution(ws),
        "ready_to_send": ready_to_send_count(ws),
        "recent_activity": recent_activity(limit=8, workspace_id=ws),
        "recent_runs": recent_runs,
        "failed_runs": [r for r in recent_runs if r.get("status") == "failed"],
    }


@app.get(
    "/api/v1/leads",
    summary="Filterable, paginated lead list",
    dependencies=[Depends(require_api_key)],
)
async def list_leads_endpoint(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    tier: str = "",
    enriched_only: bool = False,
    sent_only: bool = False,
    not_sent_only: bool = False,
) -> dict:
    from src.lib.db_queries import count_leads, list_lead_records

    ws = _default_ws(workspace_id, workspace_slug)
    tiers = [t.strip().upper() for t in tier.split(",") if t.strip()] or None
    filters = {
        "search": search,
        "tier_filter": tiers,
        "enriched_only": enriched_only,
        "sent_only": sent_only,
        "not_sent_only": not_sent_only,
    }
    limit = max(1, min(200, limit))
    rows = list_lead_records(ws, limit=limit, offset=max(0, offset), **filters)
    return {
        "leads": rows,
        "total": count_leads(ws, **filters),
        "limit": limit,
        "offset": max(0, offset),
    }


@app.get(
    "/api/v1/leads/{lead_id}",
    summary="Full detail for one lead (enrichment, score, content, signals)",
    dependencies=[Depends(require_api_key)],
)
async def get_lead_endpoint(
    lead_id: int,
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
) -> dict:
    from src.lib.db_queries import get_lead_full

    ws = _default_ws(workspace_id, workspace_slug)
    lead = get_lead_full(lead_id, ws)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead_not_found")
    return lead


@app.get(
    "/api/v1/runs",
    summary="Recent processing runs",
    dependencies=[Depends(require_api_key)],
)
async def list_runs_endpoint(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
    limit: int = 25,
) -> dict:
    ws = _default_ws(workspace_id, workspace_slug)
    return {"runs": run_store.list_recent_runs(limit=max(1, min(100, limit)), workspace_id=ws)}


@app.get(
    "/api/v1/generated-content",
    summary="Recently generated outbound content",
    dependencies=[Depends(require_api_key)],
)
async def list_generated_content(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
    kind: str = "email",
    limit: int = 25,
) -> dict:
    from sqlalchemy import select as _select

    from src.db import session_scope
    from src.models import GeneratedContent, Lead

    ws = _default_ws(workspace_id, workspace_slug)
    limit = max(1, min(100, limit))
    with session_scope() as session:
        query = (
            _select(GeneratedContent, Lead)
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .order_by(GeneratedContent.created_at.desc())
            .limit(limit)
        )
        if kind:
            query = query.where(GeneratedContent.kind == kind)
        if ws is not None:
            query = query.where(GeneratedContent.workspace_id == ws)
        items = [
            {
                "id": content.id,
                "lead_id": content.lead_id,
                "lead_name": _full_name(lead),
                "company": lead.company,
                "kind": content.kind,
                "subject": content.subject,
                "body": content.body,
                "prompt_version": content.prompt_version,
                "model": content.model,
                "skip_reason": content.skip_reason,
                "delivery_status": content.delivery_status,
                "error_message": content.error_message,
                "delivered_at": _iso(content.delivered_at),
                "created_at": _iso(content.created_at),
            }
            for content, lead in session.execute(query).all()
        ]
    return {"content": items, "kind": kind, "limit": limit}


@app.get(
    "/api/v1/settings/status",
    summary="Which configuration is present (never the values)",
    dependencies=[Depends(require_api_key)],
)
async def settings_status() -> dict:
    """Booleans and non-secret scalars only — no credential is ever returned."""
    from src.config import settings

    def configured(name: str) -> bool:
        return bool((os.environ.get(name) or "").strip())

    return {
        "env": {
            name: configured(name)
            for name in (
                "DATABASE_URL",
                "ADMIN_PASSWORD",
                "ADMIN_SESSION_SECRET",
                "INTERNAL_API_KEY",
                "ANTHROPIC_API_KEY",
                "APIFY_API_TOKEN",
                "TAVILY_API_KEY",
                "INSTANTLY_API_KEY",
                "INSTANTLY_CAMPAIGN_ID",
                "INSTANTLY_WEBHOOK_SECRET",
                "LEAD_SOURCE_JOB_SECRET",
                "CRON_SECRET",
            )
        },
        "scoring": {
            "email_verifier": settings.email_verifier,
            "tier_a_min": settings.tier_a_min,
            "tier_b_min": settings.tier_b_min,
            "send_min_tier": settings.send_min_tier,
            "scoring_model": settings.scoring_model,
            "content_model": settings.content_model,
        },
    }


@app.get(
    "/api/v1/signals",
    summary="Buying-intent signals discovered for leads",
    dependencies=[Depends(require_api_key)],
)
async def list_signals(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
    limit: int = 50,
) -> dict:
    from sqlalchemy import select as _select

    from src.db import session_scope
    from src.models import Lead, LeadSignal

    ws = _default_ws(workspace_id, workspace_slug)
    limit = max(1, min(200, limit))
    with session_scope() as session:
        query = (
            _select(LeadSignal, Lead)
            .join(Lead, Lead.id == LeadSignal.lead_id)
            .order_by(LeadSignal.id.desc())
            .limit(limit)
        )
        if ws is not None:
            query = query.where(LeadSignal.workspace_id == ws)
        signals = [
            {
                "id": signal.id,
                "lead_id": signal.lead_id,
                "lead_name": _full_name(lead),
                "company": lead.company,
                "signal_type": signal.signal_type,
                "found": bool(signal.signal_found),
                "strength": signal.signal_strength,
                "summary": signal.summary,
                "why_it_matters": signal.why_it_matters,
                "roles": list(signal.relevant_roles or []),
                "recency": signal.recency_estimate,
                "uplift": signal.tier_uplift_recommendation,
                "applied_uplift": bool(signal.applied_uplift),
                "base_tier": signal.base_tier,
                "status": signal.status,
                "error": signal.error,
                "source_urls": list(signal.source_urls or [])[:5],
                "updated_at": _iso(signal.updated_at),
            }
            for signal, lead in session.execute(query).all()
        ]
    return {"signals": signals, "total": len(signals)}


@app.get(
    "/api/v1/engagement",
    summary="Delivery and reply activity",
    dependencies=[Depends(require_api_key)],
)
async def engagement_overview(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
    limit: int = 25,
) -> dict:
    from sqlalchemy import select as _select

    from src.db import session_scope
    from src.lib.db_queries import kpi_counts, latest_instantly_snapshot
    from src.models import Engagement, GeneratedContent, Lead, ReplyThread

    ws = _default_ws(workspace_id, workspace_slug)
    limit = max(1, min(100, limit))
    with session_scope() as session:
        events_q = (
            _select(Engagement, GeneratedContent, Lead)
            .join(GeneratedContent, GeneratedContent.id == Engagement.content_id)
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .order_by(Engagement.id.desc())
            .limit(limit)
        )
        if ws is not None:
            events_q = events_q.where(Engagement.workspace_id == ws)
        events = [
            {
                "lead_id": lead.id,
                "lead_name": _full_name(lead),
                "company": lead.company,
                "subject": content.subject,
                "sent": bool(row.sent),
                "opened": bool(row.opened),
                "clicked": bool(row.clicked),
                "replied": bool(row.replied),
                "bounced": bool(row.bounced),
                "reply_sentiment": row.reply_sentiment,
                "synced_at": _iso(row.synced_at),
            }
            for row, content, lead in session.execute(events_q).all()
        ]

        replies_q = _select(ReplyThread).order_by(ReplyThread.id.desc()).limit(limit)
        if ws is not None:
            replies_q = replies_q.where(ReplyThread.workspace_id == ws)
        replies = [
            {
                "id": thread.id,
                "lead_id": thread.lead_id,
                "prospect_name": thread.prospect_name,
                "company": thread.company_name,
                "classification": thread.classification,
                "recommended_action": thread.recommended_action,
                "status": thread.status,
                "reply_text": (thread.inbound_reply_text or "")[:600],
                "received_at": _iso(thread.reply_received_at),
            }
            for thread in session.execute(replies_q).scalars().all()
        ]

    return {
        "counts": kpi_counts(ws),
        "campaign": latest_instantly_snapshot(ws),
        "events": events,
        "replies": replies,
    }


@app.get(
    "/api/v1/prompts",
    summary="Prompt overrides, recommendations and winning examples",
    dependencies=[Depends(require_api_key)],
)
async def prompts_overview(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
) -> dict:
    from sqlalchemy import select as _select

    from src.db import session_scope
    from src.models import PromptConfig, PromptRecommendation, WinningExample

    ws = _default_ws(workspace_id, workspace_slug)
    with session_scope() as session:
        def scoped(query, model):
            return query if ws is None else query.where(model.workspace_id == ws)

        configs = [
            {
                "id": row.id,
                "channel": row.channel,
                "is_active": bool(row.is_active),
                "prompt_version": row.prompt_version,
                "updated_by": row.updated_by,
                "updated_at": _iso(row.updated_at),
                "preview": (row.content or "")[:400],
                "length": len(row.content or ""),
            }
            for row in session.execute(
                scoped(_select(PromptConfig).order_by(PromptConfig.channel), PromptConfig)
            ).scalars().all()
        ]

        recommendations = [
            {
                "id": row.id,
                "channel": row.channel,
                "bottleneck": row.bottleneck,
                "diagnosis": row.diagnosis,
                "recommended_change": row.recommended_change,
                "expected_impact": row.expected_impact,
                "risk_level": row.risk_level,
                "status": row.status,
                "loop_status": row.loop_status,
                "confidence": row.confidence,
                "low_confidence": bool(row.low_confidence),
                "sample_size": row.sample_size,
                "created_at": _iso(row.created_at),
            }
            for row in session.execute(
                scoped(
                    _select(PromptRecommendation)
                    .order_by(PromptRecommendation.id.desc())
                    .limit(20),
                    PromptRecommendation,
                )
            ).scalars().all()
        ]

        winners = [
            {
                "id": row.id,
                "content_type": row.content_type,
                "subject": row.subject,
                "body": (row.body or "")[:400],
                "reply_rate": row.reply_rate,
                "manually_flagged": bool(row.manually_flagged),
                "promoted_at": _iso(row.promoted_at),
            }
            for row in session.execute(
                scoped(
                    _select(WinningExample).order_by(WinningExample.id.desc()).limit(20),
                    WinningExample,
                )
            ).scalars().all()
        ]

    return {"configs": configs, "recommendations": recommendations, "winners": winners}


@app.get(
    "/api/v1/research",
    summary="Enrichment and research outputs per lead",
    dependencies=[Depends(require_api_key)],
)
async def research_overview(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
    limit: int = 25,
) -> dict:
    from sqlalchemy import select as _select

    from src.db import session_scope
    from src.models import Enrichment, Lead

    ws = _default_ws(workspace_id, workspace_slug)
    limit = max(1, min(100, limit))
    with session_scope() as session:
        query = (
            _select(Enrichment, Lead)
            .join(Lead, Lead.id == Enrichment.lead_id)
            .order_by(Enrichment.id.desc())
            .limit(limit)
        )
        if ws is not None:
            query = query.where(Enrichment.workspace_id == ws)
        items = []
        for enrichment, lead in session.execute(query).all():
            news = enrichment.company_news if isinstance(enrichment.company_news, dict) else {}
            buyers = enrichment.buyer_accounts if isinstance(enrichment.buyer_accounts, dict) else {}
            raw_news = news.get("results") or news.get("items") or []
            raw_segments = buyers.get("segments") or []
            items.append({
                "lead_id": lead.id,
                "lead_name": _full_name(lead),
                "company": lead.company,
                "company_domain": lead.company_domain,
                "enriched_at": _iso(enrichment.enriched_at),
                "has_profile": bool(enrichment.linkedin_profile),
                "has_company": bool(enrichment.company_details),
                "has_company_news": bool(news),
                "has_industry_news": bool(enrichment.industry_news),
                "has_buyer_research": bool(buyers),
                "source_status": enrichment.source_status or {},
                "news_headlines": [
                    item.get("title")
                    for item in raw_news[:3]
                    if isinstance(item, dict) and item.get("title")
                ],
                "buyer_segments": [
                    seg.get("name") or seg.get("segment")
                    for seg in raw_segments[:4]
                    if isinstance(seg, dict)
                ],
            })
    return {"research": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Prompt editor
#
# The console edits one section at a time; a save recombines the sections into
# the full overlay and writes it through the existing loader, so the generator,
# the fingerprint and the self-improvement loop all keep working unchanged.
# ---------------------------------------------------------------------------

class PromptSection(BaseModel):
    title: str = ""
    body: str = ""


class PromptSaveRequest(BaseModel):
    channel: str = "email"
    workspace_id: int | None = None
    workspace_slug: str | None = None
    sections: list[PromptSection] = Field(default_factory=list)


_PROMPT_DEFAULTS = {
    "email": "src.prompts.email:DEFAULT_EMAIL_PROMPT_BODY",
    "linkedin_msg": "src.prompts.linkedin_msg:DEFAULT_LINKEDIN_MSG_PROMPT_BODY",
    "call_script": "src.prompts.call_script:DEFAULT_CALL_SCRIPT_PROMPT_BODY",
}


def _default_prompt_body(channel: str) -> str:
    """Import the hardcoded default for a channel. Empty string if absent."""
    target = _PROMPT_DEFAULTS.get(channel)
    if not target:
        return ""
    module_name, _, attr = target.partition(":")
    try:
        module = __import__(module_name, fromlist=[attr])
        return getattr(module, attr, "") or ""
    except Exception:  # pragma: no cover - defensive
        return ""


@app.get(
    "/api/v1/prompts/editor",
    summary="One channel's prompt split into editable sections",
    dependencies=[Depends(require_api_key)],
)
async def prompt_editor(
    channel: str = "email",
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
) -> dict:
    from src.prompts.loader import get_effective_prompt_with_source, get_overlay_metadata
    from src.prompts.sections import SUGGESTED_SECTIONS, split_sections

    if channel not in _PROMPT_DEFAULTS:
        raise HTTPException(status_code=422, detail=f"unknown channel: {channel}")

    ws = _default_ws(workspace_id, workspace_slug)
    default_body = _default_prompt_body(channel)
    try:
        text, source = get_effective_prompt_with_source(
            channel, default_body, workspace_id=ws
        )
    except Exception:
        text, source = default_body, "default"

    metadata = get_overlay_metadata(channel, ws) or {}
    sections = split_sections(text)
    present = {s["title"] for s in sections}
    return {
        "channel": channel,
        "workspace_id": ws,
        "source": source,
        "sections": sections,
        "compiled": text,
        "available_sections": [t for t in SUGGESTED_SECTIONS if t not in present],
        "metadata": {
            "updated_at": _iso(metadata.get("updated_at")),
            "updated_by": metadata.get("updated_by"),
            "prompt_version": metadata.get("prompt_version"),
            "prompt_fingerprint": metadata.get("prompt_fingerprint"),
            "is_active": metadata.get("is_active"),
        },
    }


@app.post(
    "/api/v1/prompts/editor",
    summary="Save edited prompt sections",
    dependencies=[Depends(require_api_key)],
)
async def save_prompt_editor(req: PromptSaveRequest) -> dict:
    from src.prompts.loader import get_overlay_metadata, save_overlay
    from src.prompts.sections import compile_sections

    if req.channel not in _PROMPT_DEFAULTS:
        raise HTTPException(status_code=422, detail=f"unknown channel: {req.channel}")
    if not req.sections:
        raise HTTPException(status_code=422, detail="sections must not be empty")

    ws = _default_ws(req.workspace_id, req.workspace_slug)
    compiled = compile_sections([s.model_dump() for s in req.sections])
    try:
        save_overlay(req.channel, compiled, workspace_id=ws, updated_by="web-app")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"save_failed: {exc}") from exc

    metadata = get_overlay_metadata(req.channel, ws) or {}
    return {
        "saved": True,
        "channel": req.channel,
        "length": len(compiled),
        "compiled": compiled,
        "metadata": {
            "updated_at": _iso(metadata.get("updated_at")),
            "updated_by": metadata.get("updated_by"),
            "prompt_fingerprint": metadata.get("prompt_fingerprint"),
        },
    }


# ---------------------------------------------------------------------------
# Workspace settings (ICP config)
# ---------------------------------------------------------------------------

class SettingsSaveRequest(BaseModel):
    workspace_id: int | None = None
    workspace_slug: str | None = None
    config: dict = Field(default_factory=dict)


@app.get(
    "/api/v1/settings",
    summary="Editable workspace settings (company, ICP, persona, signals)",
    dependencies=[Depends(require_api_key)],
)
async def get_settings(
    workspace_id: int | None = None,
    workspace_slug: str | None = None,
) -> dict:
    from src.delivery.instantly_config import build_instantly_diagnostic
    from src.icp_config import load_workspace_icp_config
    from src.workspace import get_workspace_by_id

    ws = _default_ws(workspace_id, workspace_slug)
    config = load_workspace_icp_config(ws)
    workspace = get_workspace_by_id(ws) if ws is not None else None

    try:
        instantly = build_instantly_diagnostic(ws)
    except Exception as exc:  # never fatal — the panel renders the reason
        instantly = {"error": f"{type(exc).__name__}: {exc}"}

    from src.config import settings as app_settings

    return {
        "workspace": {
            "id": ws,
            "name": (workspace or {}).get("name"),
            "slug": (workspace or {}).get("slug"),
        },
        "config": config.model_dump(),
        # Presence + masked prefix only; the key itself is never returned.
        "instantly": instantly,
        "deliverability": {
            "verifier": app_settings.email_verifier,
            "verifier_key_configured": bool(
                (os.environ.get("INSTANTLY_API_KEY") if app_settings.email_verifier == "instantly"
                 else os.environ.get(f"{app_settings.email_verifier.upper()}_API_KEY")) or ""
            ),
        },
    }


@app.post(
    "/api/v1/settings",
    summary="Save workspace settings",
    dependencies=[Depends(require_api_key)],
)
async def save_settings(req: SettingsSaveRequest) -> dict:
    from src.icp_config import ICPConfig, load_workspace_icp_config, save_workspace_icp_config

    ws = _default_ws(req.workspace_id, req.workspace_slug)
    current = load_workspace_icp_config(ws).model_dump()
    # Merge one level deep so a partial save (one accordion) never wipes the rest.
    for key, value in (req.config or {}).items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        else:
            current[key] = value

    try:
        merged = ICPConfig.model_validate(current)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_settings: {exc}") from exc

    try:
        save_workspace_icp_config(merged, ws)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"save_failed: {exc}") from exc

    return {"saved": True, "workspace_id": ws, "config": merged.model_dump()}


class InstantlySaveRequest(BaseModel):
    workspace_id: int | None = None
    workspace_slug: str | None = None
    campaign_id: str = ""


@app.post(
    "/api/v1/settings/instantly",
    summary="Save the workspace Instantly campaign id",
    dependencies=[Depends(require_api_key)],
)
async def save_instantly_settings(req: InstantlySaveRequest) -> dict:
    """Campaign id only. The API key is shared infrastructure and is never
    written from the console."""
    from src.db import session_scope
    from src.models import Workspace

    ws = _default_ws(req.workspace_id, req.workspace_slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace_not_found")
    with session_scope() as session:
        workspace = session.get(Workspace, ws)
        if workspace is None:
            raise HTTPException(status_code=404, detail="workspace_not_found")
        workspace.instantly_campaign_id = (req.campaign_id or "").strip() or None
    return {"saved": True, "workspace_id": ws, "campaign_id": req.campaign_id}


# ---------------------------------------------------------------------------
# Admin session (console login)
#
# The console signs in with ADMIN_PASSWORD and receives an HttpOnly cookie.
# ADMIN_PASSWORD is never returned, and INTERNAL_API_KEY never reaches the
# browser at all.
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str = ""


@app.post("/api/auth/login", summary="Sign in with the admin password")
async def auth_login(req: LoginRequest, response: Response) -> dict:
    if not admin_password():
        log.error("admin_password_not_configured")
        raise HTTPException(
            status_code=500,
            detail="ADMIN_PASSWORD is not configured on this server.",
        )
    if not password_matches(req.password or ""):
        log.warning("admin_login_failed")
        raise HTTPException(status_code=401, detail="Incorrect password.")

    try:
        token = issue_token()
    except AuthNotConfigured as exc:  # pragma: no cover - guarded above
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response.set_cookie(value=token, **cookie_kwargs())
    log.info("admin_login_ok")
    return {"authenticated": True}


@app.post("/api/auth/logout", summary="Clear the admin session")
async def auth_logout(response: Response) -> dict:
    # Overwrite with an immediately-expiring cookie so it is dropped even where
    # delete_cookie's attributes would not match.
    response.set_cookie(value="", **{**cookie_kwargs(), "max_age": 0})
    return {"authenticated": False}


@app.get("/api/auth/me", summary="Is this browser signed in?")
async def auth_me(request: Request) -> dict:
    return {
        "authenticated": verify_token(request.cookies.get(COOKIE_NAME)),
        "login_configured": bool(admin_password()),
    }
