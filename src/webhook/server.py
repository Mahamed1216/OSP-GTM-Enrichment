"""FastAPI webhook receiver for Instantly Lead Replied automation.

Endpoint: POST /api/instantly/reply-webhook
Security: X-Webhook-Secret header must match INSTANTLY_WEBHOOK_SECRET env var.

On Vercel this is mounted by api/index.py; the reply webhook is reachable at
POST /api/instantly/reply-webhook. Run it standalone for local development:

    uvicorn api.index:app --port 8000
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

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


def _secret_matches(provided: str | None, expected: str) -> bool:
    """Constant-time secret comparison.

    Returns False when either value is empty (never authenticate against an
    unset secret), otherwise compares with ``hmac.compare_digest`` so the
    check does not leak length/content via timing.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Abuse guards (H5): body-size limit + simple per-IP rate limiting.
#
# State is in-process — appropriate for this single, separately-deployed
# webhook server (one process). Limits are env-configurable so deployments and
# tests can tune them. These run as HTTP middleware BEFORE routing, so an
# oversized or rate-limited request is rejected before body parsing, the
# secret check, Anthropic calls, or DB writes.
# ---------------------------------------------------------------------------

_DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MiB
_DEFAULT_RATE_LIMIT = 240            # requests per window per client IP
_DEFAULT_RATE_WINDOW = 60.0          # seconds

_rate_lock = threading.Lock()
_rate_buckets: dict[str, tuple[float, int]] = {}  # client -> (window_start, count)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _max_body_bytes() -> int:
    return _env_int("WEBHOOK_MAX_BODY_BYTES", _DEFAULT_MAX_BODY_BYTES)


def _rate_limit_max() -> int:
    return _env_int("WEBHOOK_RATE_LIMIT", _DEFAULT_RATE_LIMIT)


def _rate_limit_window() -> float:
    try:
        return float(os.environ.get("WEBHOOK_RATE_WINDOW", str(_DEFAULT_RATE_WINDOW)))
    except (TypeError, ValueError):
        return _DEFAULT_RATE_WINDOW


def reset_rate_limit_state() -> None:
    """Clear all rate-limit buckets (used by tests for isolation)."""
    with _rate_lock:
        _rate_buckets.clear()


def _rate_limited(client: str) -> bool:
    """Fixed-window per-client limiter. Returns True if this request exceeds
    the configured limit for the current window."""
    limit = _rate_limit_max()
    window = _rate_limit_window()
    now = time.monotonic()
    with _rate_lock:
        start, count = _rate_buckets.get(client, (now, 0))
        if now - start >= window:
            start, count = now, 0  # window expired — reset
        count += 1
        _rate_buckets[client] = (start, count)
        return count > limit


@app.middleware("http")
async def _abuse_guards(request: Request, call_next):
    """Reject oversized / rate-limited POSTs before any expensive processing."""
    if request.method == "POST":
        # 1. Body-size limit (Content-Length based; rejects before body parse).
        #    Require a parseable Content-Length so the size guard cannot be
        #    bypassed by omitting the header (e.g. chunked transfer). Missing or
        #    malformed -> 411; present but over the cap -> 413.
        content_length = request.headers.get("content-length")
        if content_length is None:
            return JSONResponse(
                status_code=411,
                content={"detail": "Content-Length header is required."},
            )
        try:
            declared_length = int(content_length)
        except ValueError:
            return JSONResponse(
                status_code=411,
                content={"detail": "Content-Length header is invalid."},
            )
        if declared_length > _max_body_bytes():
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large (limit {_max_body_bytes()} bytes)."},
            )
        # 2. Per-IP rate limit.
        client = request.client.host if request.client else "unknown"
        if _rate_limited(client):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
            )
    return await call_next(request)


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
    if not _secret_matches(x_webhook_secret, expected):
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
    if not _secret_matches(x_job_secret, expected):
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
