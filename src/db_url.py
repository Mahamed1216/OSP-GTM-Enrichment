"""Describe the configured DATABASE_URL without ever exposing credentials.

The app reads `DATABASE_URL` and hands it to SQLAlchemy unchanged apart from a
`postgres://` -> `postgresql://` scheme fix (SQLAlchemy 2.0 has no `postgres`
dialect). Nothing here or anywhere else rewrites the host or the port —
`describe_database_url` exists so that claim is checkable from a running
deployment instead of taken on trust.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

# Supabase's transaction/session pooler hostnames.
_POOLER_MARKER = ".pooler.supabase.com"
# Supabase's direct-connection hostname: db.<project-ref>.supabase.co
_DIRECT_RE = re.compile(r"^db\.[a-z0-9]+\.supabase\.co$", re.IGNORECASE)
# Pooler usernames look like postgres.<project-ref>.
_POOLER_USER_RE = re.compile(r"^postgres\.[a-z0-9]{8,}$", re.IGNORECASE)

POOLER_PORTS = {5432, 6543}


def _user_shape(user: str | None) -> str | None:
    """Classify the username without revealing it."""
    if not user:
        return None
    user = unquote(user)
    if _POOLER_USER_RE.match(user):
        return "postgres.<project-ref>"
    if user == "postgres":
        return "postgres"
    return "unknown"


def describe_database_url(raw: str | None) -> dict:
    """Redacted summary of a connection string.

    Returns host and port (public DNS facts, not secrets) and the *shape* of
    the username. Never returns the username, the password, or the raw URL.
    """
    url = (raw or "").strip()
    if not url:
        return {
            "database_configured": False,
            "database_scheme": None,
            "database_host": None,
            "database_port": None,
            "database_uses_pooler": False,
            "database_user_shape": None,
            "database_warning": None,
        }

    # Normalise the scheme the same way src.db does, so the summary describes
    # what SQLAlchemy actually receives.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").split("+")[0] or None
        host = parts.hostname
        port = parts.port
        user = parts.username
    except Exception:
        return {
            "database_configured": True,
            "database_scheme": None,
            "database_host": None,
            "database_port": None,
            "database_uses_pooler": False,
            "database_user_shape": "unknown",
            "database_warning": "DATABASE_URL could not be parsed.",
        }

    if scheme == "sqlite":
        return {
            "database_configured": True,
            "database_scheme": "sqlite",
            "database_host": None,
            "database_port": None,
            "database_uses_pooler": False,
            "database_user_shape": None,
            "database_warning": (
                "DATABASE_URL is SQLite. On a serverless host the filesystem is "
                "read-only; set a Postgres connection string."
            ),
        }

    host_lower = (host or "").lower()
    uses_pooler = _POOLER_MARKER in host_lower
    is_direct = bool(_DIRECT_RE.match(host_lower))

    warning = None
    if is_direct and port in POOLER_PORTS and port == 6543:
        warning = (
            "The running app is using a direct Supabase host with a pooler "
            "port. This usually means the deployed DATABASE_URL is not the "
            "Transaction Pooler URL, or an older value is still set for this "
            "environment. The pooler host looks like "
            "aws-0-<region>.pooler.supabase.com."
        )
    elif is_direct:
        warning = (
            "The running app is using the direct Supabase host "
            "(db.<project-ref>.supabase.co). Serverless functions should use "
            "the Transaction Pooler URL instead — direct connections exhaust "
            "the connection limit."
        )

    return {
        "database_configured": True,
        "database_scheme": scheme,
        "database_host": host,
        "database_port": port,
        "database_uses_pooler": uses_pooler,
        "database_user_shape": _user_shape(user),
        "database_warning": warning,
    }


def connection_summary(raw: str | None) -> str:
    """One-line, credential-free summary for logs."""
    info = describe_database_url(raw)
    return (
        f"host={info['database_host']} port={info['database_port']} "
        f"uses_pooler={info['database_uses_pooler']}"
    )
