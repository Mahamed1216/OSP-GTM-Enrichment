"""Vercel serverless entrypoint (ASGI).

Vercel routes every request to this one Python function (see ``vercel.json``
rewrites). A small ASGI dispatcher fronts the two FastAPI apps this repo
already has, so their routes, middleware and auth are reused unchanged:

    /api/instantly/*    -> src.webhook.server:app  (Instantly reply webhook)
    /api/lead-source/*  -> src.webhook.server:app  (lead-source scheduler)
    /health, /api/v1/*  -> src.api.server:app      (internal API)
    /api/v1/drain       -> drains queued async runs (the serverless stand-in
                           for ``python -m src.api.worker``, which has no
                           long-lived process to run in here)

``/`` and every other path belong to the Next.js operator console (``pages/``);
Next.js rewrites only /health and /api/* to this function — see next.config.js.

The Streamlit UI in ``app/`` is deliberately NOT served here: Streamlit needs a
long-lived, stateful websocket server and cannot run on Vercel. Keep it on
Streamlit Cloud (or any container host) pointed at the same DATABASE_URL.

Local check:
    uvicorn api.index:app --port 8000
"""
from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

# The function's entrypoint is api/index.py, so the repo root must be on
# sys.path for `import src.*` / `import app.lib.*` to resolve.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # no-op on Vercel (env vars come from the project settings)

from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from src.api.server import app as _api_app  # noqa: E402
from src.webhook.server import app as _webhook_app  # noqa: E402

_WEBHOOK_PREFIXES = ("/api/instantly", "/api/lead-source")
_DRAIN_PATH = "/api/v1/drain"
# "/" is served by the Next.js UI in production; /api/info is how that UI reads
# the same service summary. Booleans only — never a secret value.
_INFO_PATHS = ("/", "/api/info")


# ---------------------------------------------------------------------------
# Adapter-only routes
# ---------------------------------------------------------------------------

async def _info(scope, receive, send) -> None:
    """Service summary for the operator console (and local `uvicorn` runs)."""
    await JSONResponse({
        "service": "osp-gtm-enrichment",
        "status": "ok",
        "database_configured": bool(os.environ.get("DATABASE_URL")),
        "endpoints": {
            "health": "GET /health",
            "info": "GET /api/info",
            "process": "POST /api/v1/leads/process",
            "run_status": "GET /api/v1/runs/{run_id}",
            "drain_queued": "POST /api/v1/drain",
            "instantly_webhook": "POST /api/instantly/reply-webhook",
            "lead_source_scheduler": "POST /api/lead-source/run-scheduled",
        },
    })(scope, receive, send)


def _authorized(request: Request) -> bool:
    """Bearer INTERNAL_API_KEY, or CRON_SECRET when called by a Vercel Cron."""
    header = request.headers.get("authorization") or ""
    parts = header.split(" ", 1)
    token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    if not token:
        return False
    for expected in (os.environ.get("INTERNAL_API_KEY", ""), os.environ.get("CRON_SECRET", "")):
        if expected and hmac.compare_digest(token, expected):
            return True
    return False


async def _drain(scope, receive, send) -> None:
    """Process queued api_runs. GET and POST both work so a Vercel Cron can
    call it. Keep the batch small — this shares the function's maxDuration."""
    request = Request(scope, receive)
    if not _authorized(request):
        await JSONResponse({"detail": "Invalid or missing bearer token."}, status_code=401)(
            scope, receive, send)
        return
    try:
        batch = max(1, min(10, int(request.query_params.get("batch", "3"))))
    except ValueError:
        batch = 3

    from src.api.worker import drain_once

    processed = await drain_once(batch=batch)
    await JSONResponse({"processed": processed, "batch": batch})(scope, receive, send)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def _dispatch(scope, receive, send) -> None:
    if scope["type"] == "lifespan":
        # Only the internal API defines one (idempotent init_db). Vercel does
        # not always run the lifespan protocol; nothing here depends on it.
        await _api_app(scope, receive, send)
        return

    path = scope.get("path") or "/"
    if path == _DRAIN_PATH:
        await _drain(scope, receive, send)
    elif path.startswith(_WEBHOOK_PREFIXES):
        await _webhook_app(scope, receive, send)
    elif path in _INFO_PATHS:
        await _info(scope, receive, send)
    else:
        await _api_app(scope, receive, send)


# Wrapped in a Starlette app so Vercel's runtime sees an ASGI application
# instance. Mount("/") passes the original path through untouched.
app = Starlette(routes=[Mount("/", app=_dispatch)])
