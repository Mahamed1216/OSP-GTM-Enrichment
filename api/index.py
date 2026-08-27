"""Vercel serverless entrypoint (ASGI).

Serves ``/health`` and ``/api/*``. The UI at ``/`` is Next.js and never reaches
this file — see next.config.js.

    /                   -> NOT handled here; the Next.js UI owns the homepage
    /api/auth/*         -> src.api.server:app      (admin console login)
    /health             -> answered here; reports backend + database status
    /api/info           -> answered here; booleans only, never a secret
    /api/instantly/*    -> src.webhook.server:app  (Instantly reply webhook)
    /api/lead-source/*  -> src.webhook.server:app  (lead-source scheduler)
    /api/v1/drain       -> drains queued async runs (the serverless stand-in
                           for ``python -m src.api.worker``)
    /api/v1/*           -> src.api.server:app      (internal API)

Failure policy: nothing here may raise out of the ASGI callable. A crash out of
a Vercel function is an opaque FUNCTION_INVOCATION_FAILED page, so the backend
is imported lazily and every failure is turned into a JSON body that names the
cause. ``/health`` keeps answering even when the backend cannot be imported at
all, which is what makes the deployment diagnosable from the outside.

Local check:
    uvicorn api.index:app --port 8000
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import traceback
from pathlib import Path

# The entrypoint is api/index.py, so the repo root must be on sys.path for
# `import src.*` to resolve. vercel.json's includeFiles is what puts src/ in
# the bundle in the first place.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SERVICE = "osp-gtm-enrichment"

# "/" is NOT listed: the operator UI owns the homepage. If a request for "/"
# ever reaches this function, the Next.js deployment is broken and a 404 here is
# the honest signal — answering JSON at "/" would hide it.
_INFO_PATHS = ("/api/info",)
_HEALTH_PATH = "/health"
_DRAIN_PATH = "/api/v1/drain"
_WEBHOOK_PREFIXES = ("/api/instantly", "/api/lead-source")
# Everything that touches the database. On Vercel these are refused with a
# clean 503 when DATABASE_URL is unset, because the default sqlite:///sdr.db
# would land on a read-only filesystem and fail deep inside a query instead.
_DB_PREFIXES = ("/api/v1", "/api/instantly", "/api/lead-source")


# ---------------------------------------------------------------------------
# Raw-ASGI helpers — deliberately no framework imports, so this file can still
# answer with JSON when the backend (or even fastapi) fails to import.
# ---------------------------------------------------------------------------

async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _header(scope, name: str) -> str | None:
    target = name.lower().encode("ascii")
    for key, value in scope.get("headers") or []:
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _query_int(scope, name: str, default: int, lo: int, hi: int) -> int:
    raw = (scope.get("query_string") or b"").decode("latin-1")
    for part in raw.split("&"):
        key, _, value = part.partition("=")
        if key == name:
            try:
                return max(lo, min(hi, int(value)))
            except ValueError:
                return default
    return default


# ---------------------------------------------------------------------------
# Lazy backend import
# ---------------------------------------------------------------------------

_BACKEND: dict = {"loaded": False, "api": None, "webhook": None, "error": None}


def _backend() -> dict:
    """Import the two FastAPI apps once. Never raises — records the error."""
    if _BACKEND["loaded"]:
        return _BACKEND
    _BACKEND["loaded"] = True
    try:
        try:
            from dotenv import load_dotenv
            load_dotenv()  # no-op on Vercel; convenient for local runs
        except Exception:
            pass
        from src.api.server import app as api_app
        from src.webhook.server import app as webhook_app
        _BACKEND["api"] = api_app
        _BACKEND["webhook"] = webhook_app
    except Exception as exc:
        _BACKEND["error"] = {
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "The Python function could not import the pipeline code. If this "
                "is ModuleNotFoundError for 'src', the bundle is missing those "
                "files — check includeFiles in vercel.json. If it names a "
                "third-party package, it is missing from [project].dependencies "
                "in pyproject.toml, which is what Vercel's uv installs."
            ),
            "traceback": traceback.format_exc().splitlines()[-6:],
        }
    return _BACKEND


def _database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def _on_vercel() -> bool:
    return bool(os.environ.get("VERCEL"))


# ---------------------------------------------------------------------------
# Routes owned by this adapter
# ---------------------------------------------------------------------------

async def _health(scope, receive, send) -> None:
    """Never fails. Reports whether the backend and database are usable."""
    backend = _backend()
    db_set = bool(_database_url())
    healthy = backend["error"] is None and (db_set or not _on_vercel())
    payload = {
        "status": "ok" if healthy else "degraded",
        "service": SERVICE,
        "version": "v1",
        "backend_importable": backend["error"] is None,
        "database_configured": db_set,
    }
    if backend["error"] is not None:
        payload["backend_error"] = backend["error"]["error"]
    if not db_set and _on_vercel():
        payload["database_error"] = "DATABASE_URL not configured"
    await _send_json(send, 200 if healthy else 503, payload)


def _database_facts() -> dict:
    """Non-identifying connection facts: scheme, port, pooler yes/no.

    The host and username shape are deliberately NOT here — /api/info is
    public, and the host carries the Supabase project ref. The full summary
    lives behind auth on /api/v1/settings/status.
    """
    try:
        from src.db_url import describe_database_url
        info = describe_database_url(_database_url())
        return {
            "database_scheme": info["database_scheme"],
            "database_port": info["database_port"],
            "database_uses_pooler": info["database_uses_pooler"],
            "database_warning": info["database_warning"],
        }
    except Exception:
        return {}


async def _info(scope, receive, send) -> None:
    backend = _backend()
    await _send_json(send, 200, {
        "service": SERVICE,
        "status": "ok" if backend["error"] is None else "degraded",
        "backend_importable": backend["error"] is None,
        "backend_error": None if backend["error"] is None else backend["error"]["error"],
        "database_configured": bool(_database_url()),
        **_database_facts(),
        "endpoints": {
            "health": "GET /health",
            "info": "GET /api/info",
            "process": "POST /api/v1/leads/process",
            "run_status": "GET /api/v1/runs/{run_id}",
            "drain_queued": "POST /api/v1/drain",
            "instantly_webhook": "POST /api/instantly/reply-webhook",
            "lead_source_scheduler": "POST /api/lead-source/run-scheduled",
        },
    })


def _cookie(scope, name: str) -> str | None:
    """One cookie value from the raw Cookie header."""
    header = _header(scope, "cookie") or ""
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _admin_session(scope) -> bool:
    """True when the request carries a valid console session cookie.

    Imported lazily and guarded: this module must keep answering even when the
    backend cannot be imported at all.
    """
    try:
        from src.api.auth import COOKIE_NAME, verify_token
    except Exception:
        return False
    return verify_token(_cookie(scope, COOKIE_NAME))


def _authorized(scope) -> bool:
    """An admin console session, a bearer INTERNAL_API_KEY, or CRON_SECRET.

    The console signs in with ADMIN_PASSWORD and reaches this through the
    cookie; cron and scripts keep using a bearer token.
    """
    if _admin_session(scope):
        return True
    parts = (_header(scope, "authorization") or "").split(" ", 1)
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
    if not _authorized(scope):
        await _send_json(send, 401, {"detail": "Invalid or missing bearer token."})
        return
    batch = _query_int(scope, "batch", 3, 1, 10)
    try:
        from src.api.worker import drain_once
        processed = await drain_once(batch=batch)
    except Exception as exc:
        await _send_json(send, 500, {
            "error": f"{type(exc).__name__}: {exc}",
            "detail": "drain_failed",
        })
        return
    await _send_json(send, 200, {"processed": processed, "batch": batch})


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def _dispatch(scope, receive, send) -> None:
    if scope["type"] == "lifespan":
        # Vercel does not run the lifespan protocol and nothing here depends on
        # it. Answer it correctly for local servers that do.
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope["type"] != "http":
        return

    path = scope.get("path") or "/"

    if path == _HEALTH_PATH:
        await _health(scope, receive, send)
        return
    if path in _INFO_PATHS:
        await _info(scope, receive, send)
        return

    # Anything below needs the database. Fail cleanly rather than deep inside a
    # query against a read-only sqlite file.
    if _on_vercel() and path.startswith(_DB_PREFIXES) and not _database_url():
        await _send_json(send, 503, {
            "error": "DATABASE_URL not configured",
            "detail": (
                "Set DATABASE_URL to a Postgres connection string in the Vercel "
                "project's environment variables, then redeploy. See "
                "supabase/README.md."
            ),
        })
        return

    if path == _DRAIN_PATH:
        await _drain(scope, receive, send)
        return

    backend = _backend()
    if backend["error"] is not None:
        await _send_json(send, 503, backend["error"])
        return

    target = backend["webhook"] if path.startswith(_WEBHOOK_PREFIXES) else backend["api"]
    try:
        await target(scope, receive, send)
    except Exception as exc:
        # The FastAPI apps handle their own errors; this is the last line of
        # defence so a crash never becomes an opaque Vercel 500 page.
        await _send_json(send, 500, {
            "error": f"{type(exc).__name__}: {exc}",
            "detail": "unhandled_error",
        })


class _AsgiApp:
    """Plain ASGI app object — no framework import, so this module stays
    importable (and able to report the problem) even if a dependency is
    missing from the bundle."""

    async def __call__(self, scope, receive, send) -> None:
        await _dispatch(scope, receive, send)


app = _AsgiApp()
