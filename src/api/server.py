"""Internal API for SalesOS.

SalesOS sends lead payloads (sourced via Dele's system) here; we run the
existing pipeline (enrichment, buyer research, signal capture, scoring, email,
safety) and return the processed payload for SalesOS to route. We NEVER push to
Instantly and NEVER send email.

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

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from src.api import run_store
from src.api.processing import build_processed_payload, normalize_options, process_run

log = logging.getLogger(__name__)

API_VERSION = "v1"
SERVICE_NAME = "osp-gtm-enrichment"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Ensure schema exists when this runs as a standalone container against the
    # shared DB. Guarded + idempotent; never fatal at boot.
    try:
        from src.db import init_db
        init_db()
    except Exception as exc:  # pragma: no cover - defensive boot guard
        log.warning("api_init_db_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
    yield


app = FastAPI(
    title="OSP GTM Enrichment — Internal API",
    description="SalesOS-facing API to process sourced leads through the pipeline.",
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


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: enforce a valid bearer key. Never logs the key."""
    expected = _expected_api_key()
    if not expected:
        log.error("internal_api_key_not_configured")
        raise HTTPException(
            status_code=500,
            detail="INTERNAL_API_KEY is not configured on this server.",
        )
    provided = _bearer_token(authorization)
    if not provided or not hmac.compare_digest(provided, expected):
        # Log presence only — never the value.
        log.warning("api_auth_failed", extra={"token_present": bool(provided)})
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProcessRequest(BaseModel):
    workspace_slug: str | None = None
    workspace_id: int | None = None
    source: str | None = "salesos"
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
    summary="Process one lead or a batch (from SalesOS)",
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
