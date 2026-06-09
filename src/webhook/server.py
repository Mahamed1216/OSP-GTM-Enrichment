"""FastAPI webhook receiver for Instantly Lead Replied automation.

Endpoint: POST /api/instantly/reply-webhook
Security: X-Webhook-Secret header must match INSTANTLY_WEBHOOK_SECRET env var.

Run alongside Streamlit:
    python run_webhook.py                   # dev (port 8001)
    uvicorn src.webhook.server:app --port 8001  # production

Streamlit Cloud note: Streamlit Cloud runs only one Streamlit process.
Deploy this server separately (Railway, Fly.io, Render, etc.) using the
same DATABASE_URL so it shares the Postgres DB with the Streamlit app.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Header, HTTPException

from pydantic import BaseModel

from src.webhook.handler import WebhookPayload, WebhookResult, handle_reply_webhook

log = logging.getLogger(__name__)

app = FastAPI(
    title="OSP Reply Webhook Receiver",
    description="Receives Instantly Lead Replied webhooks and queues them for the Reply Agent.",
    version="1.0",
)


def _expected_secret() -> str:
    """Read secret fresh from env so tests can override with monkeypatch.setenv."""
    return os.environ.get("INSTANTLY_WEBHOOK_SECRET", "")


def _expected_job_secret() -> str:
    """Job secret for the lead-source scheduler endpoint."""
    return os.environ.get("LEAD_SOURCE_JOB_SECRET", "")


@app.post(
    "/api/instantly/reply-webhook",
    response_model=WebhookResult,
    summary="Receive an Instantly Lead Replied webhook",
)
async def reply_webhook(
    payload: WebhookPayload,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> WebhookResult:
    """Validate the webhook secret, then classify and queue the inbound reply.

    - Returns 401 if X-Webhook-Secret is missing or incorrect.
    - Returns 200 with ok=true on success (even for missing_reply_text).
    - Never auto-sends a reply.
    """
    expected = _expected_secret()
    if not expected:
        log.error("webhook_secret_not_configured")
        raise HTTPException(
            status_code=500,
            detail="INSTANTLY_WEBHOOK_SECRET is not configured on this server.",
        )
    if not x_webhook_secret or x_webhook_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret.")

    return await handle_reply_webhook(payload)


@app.get("/health", summary="Health check")
async def health() -> dict:
    return {"ok": True, "service": "osp-reply-webhook"}


# ---------------------------------------------------------------------------
# Option B: Lead Source evergreen scheduler endpoint
# ---------------------------------------------------------------------------

class SchedulerRequest(BaseModel):
    workspace_id: int | None = None
    dry_run: bool = False


class SchedulerResult(BaseModel):
    ok: bool
    results: list[dict] = []
    error: str | None = None


@app.post(
    "/api/lead-source/run-scheduled",
    response_model=SchedulerResult,
    summary="Trigger evergreen lead source import for enabled workspaces",
)
async def run_lead_source_scheduled(
    body: SchedulerRequest | None = None,
    x_job_secret: str | None = Header(default=None, alias="X-Job-Secret"),
) -> SchedulerResult:
    """Run the lead source scheduler for all (or one) enabled workspace(s).

    - Requires X-Job-Secret header matching LEAD_SOURCE_JOB_SECRET env var.
    - Returns 401 if the secret is missing or wrong.
    - Returns 500 if LEAD_SOURCE_JOB_SECRET is not configured.
    - Never auto-sends emails, never pushes to Instantly, never calls POST /runs.
    """
    expected = _expected_job_secret()
    if not expected:
        log.error("job_secret_not_configured")
        raise HTTPException(
            status_code=500,
            detail="LEAD_SOURCE_JOB_SECRET is not configured on this server.",
        )
    if not x_job_secret or x_job_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Job-Secret.")

    from src.lead_source.scheduler import run_all_enabled_workspaces

    workspace_id = body.workspace_id if body else None
    dry_run = body.dry_run if body else False

    try:
        results = await run_all_enabled_workspaces(
            workspace_id=workspace_id,
            dry_run=dry_run,
        )
        return SchedulerResult(ok=True, results=results)
    except Exception as exc:
        log.warning(
            "scheduler_endpoint_error",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        return SchedulerResult(ok=False, error=f"{type(exc).__name__}: {exc}")
