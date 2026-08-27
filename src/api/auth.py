"""Admin session auth for the operator console.

The console signs in with ADMIN_PASSWORD and gets an HttpOnly cookie. The
browser never sees, stores or sends INTERNAL_API_KEY — that stays a
server-side credential for backend-to-backend callers.

Sessions are a signed token rather than server state: the API runs as
short-lived serverless functions with no shared memory, so there is nowhere to
keep a session table. The token carries only an expiry; there is nothing
sensitive in it, and the signature is what makes it unforgeable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

COOKIE_NAME = "signalos_session"
DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12 hours


class AuthNotConfigured(RuntimeError):
    """ADMIN_PASSWORD is not set, so no one can sign in."""


def admin_password() -> str:
    return (os.environ.get("ADMIN_PASSWORD") or "").strip()


def session_secret() -> str:
    """Signing key for session tokens.

    Falls back to a value derived from ADMIN_PASSWORD so the app works with
    ADMIN_PASSWORD alone. A useful side effect: changing the password
    invalidates every existing session.
    """
    explicit = (os.environ.get("ADMIN_SESSION_SECRET") or "").strip()
    if explicit:
        return explicit
    password = admin_password()
    if not password:
        raise AuthNotConfigured("ADMIN_PASSWORD is not configured on this server.")
    return hashlib.sha256(f"signalos-session:{password}".encode("utf-8")).hexdigest()


def password_matches(candidate: str) -> bool:
    """Constant-time comparison. False when no password is configured."""
    expected = admin_password()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256)
    return _b64encode(digest.digest())


def issue_token(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a signed session token valid for ``ttl_seconds``."""
    secret = session_secret()
    payload = _b64encode(
        json.dumps({"exp": int(time.time()) + ttl_seconds, "v": 1}).encode("utf-8")
    )
    return f"{payload}.{_sign(payload, secret)}"


def verify_token(token: str | None) -> bool:
    """True only for a well-formed, correctly signed, unexpired token."""
    if not token or "." not in token:
        return False
    payload, _, signature = token.partition(".")
    try:
        secret = session_secret()
    except AuthNotConfigured:
        return False
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return False
    try:
        claims = json.loads(_b64decode(payload))
    except Exception:
        return False
    expiry = claims.get("exp")
    return isinstance(expiry, int) and expiry > int(time.time())


def cookie_kwargs(max_age: int = DEFAULT_TTL_SECONDS) -> dict:
    """Cookie flags. `secure` is off locally so http://localhost still works."""
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": bool(os.environ.get("VERCEL")),
        "path": "/",
        "max_age": max_age,
    }
