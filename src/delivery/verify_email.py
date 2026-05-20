"""Email verification — Instantly first, fall back to NeverBounce / MillionVerifier.

Result is cached on the Lead row (status, provider, verified_at) so re-runs
don't burn through verifier credits.
"""
import logging
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import select

from src.config import settings
from src.db import session_scope
from src.models import Lead
from src.retry import retry_api

log = logging.getLogger(__name__)

VerifyStatus = Literal["valid", "risky", "invalid", "unknown"]


class VerifyResult(BaseModel):
    status: VerifyStatus
    provider: str
    raw: dict


_CLIENT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Vendor strings → canonical 4-bucket VerifyStatus. Apollo writes "Verified" /
# "Email No Longer Verified", NeverBounce uses "valid"/"invalid"/"catch_all",
# MillionVerifier uses "ok"/"deliverable". Anything not listed falls through
# to "unknown" rather than raising — Pydantic Literal can't accept raw vendor
# strings, so EVERY VerifyResult construction must funnel through this.
_INVALID_TOKENS = (
    "invalid", "undeliverable", "hard_bounce", "rejected", "bounced",
    "bad", "email no longer verified", "no longer",
)
_RISKY_TOKENS = (
    "risky", "catch_all", "catch-all", "catchall", "accept_all",
    "unknown_catch_all", "disposable", "role",
)
_VALID_TOKENS = ("verified", "valid", "ok", "deliverable")


def normalize_status(raw: str | None) -> VerifyStatus:
    """Map any vendor or cached status string to a VerifyStatus literal.
    Safe to call at every VerifyResult construction site."""
    if not raw:
        return "unknown"
    text = str(raw).strip().lower()
    if not text or text == "unknown":
        return "unknown"
    # Order matters: "email no longer verified" contains "verified", but
    # the explicit invalid match must win.
    if any(t in text for t in _INVALID_TOKENS):
        return "invalid"
    if any(t in text for t in _RISKY_TOKENS):
        return "risky"
    if any(t in text for t in _VALID_TOKENS):
        return "valid"
    return "unknown"


def _normalize(provider: str, raw: dict, fallback: str = "unknown") -> VerifyStatus:
    """Pull candidate status strings from a vendor response dict and
    funnel through `normalize_status`."""
    candidates = []
    for key in ("status", "result", "verification_status", "code", "deliverability"):
        v = raw.get(key)
        if isinstance(v, str):
            candidates.append(v.strip())
    if not candidates:
        return normalize_status(fallback)
    # Try each candidate; first non-"unknown" wins.
    for c in candidates:
        result = normalize_status(c)
        if result != "unknown":
            return result
    return normalize_status(fallback)


@retry_api
async def _verify_instantly(email: str) -> VerifyResult:
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.post(
            "https://api.instantly.ai/api/v2/email-verification",
            headers={"Authorization": f"Bearer {settings.instantly_api_key}"},
            json={"email": email},
        )
        resp.raise_for_status()
        raw = resp.json()
    return VerifyResult(status=_normalize("instantly", raw), provider="instantly", raw=raw)


@retry_api
async def _verify_neverbounce(email: str) -> VerifyResult:
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.get(
            "https://api.neverbounce.com/v4/single/check",
            params={"key": settings.neverbounce_api_key, "email": email},
        )
        resp.raise_for_status()
        raw = resp.json()
    return VerifyResult(status=_normalize("neverbounce", raw), provider="neverbounce", raw=raw)


@retry_api
async def _verify_millionverifier(email: str) -> VerifyResult:
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        resp = await client.get(
            "https://api.millionverifier.com/api/v3/",
            params={"api": settings.millionverifier_api_key, "email": email},
        )
        resp.raise_for_status()
        raw = resp.json()
    return VerifyResult(status=_normalize("millionverifier", raw), provider="millionverifier", raw=raw)


_PROVIDERS = {
    "instantly": _verify_instantly,
    "neverbounce": _verify_neverbounce,
    "millionverifier": _verify_millionverifier,
}


async def verify_email(lead_id: int, *, force: bool = False) -> VerifyResult:
    """Verify a lead's email with caching. Returns cached result unless force=True."""
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        email = lead.email
        cached_status = lead.email_verification_status
        cached_provider = lead.email_verification_provider

    if cached_status and not force:
        normalized = normalize_status(cached_status)
        log.info("verify_email_cached", extra={
            "lead_id": lead_id, "raw_status": cached_status,
            "status": normalized, "provider": cached_provider,
        })
        return VerifyResult(
            status=normalized,
            provider=cached_provider or "cache",
            raw={"cached": True, "raw_status": cached_status},
        )

    verifier = _PROVIDERS.get(settings.email_verifier)
    if verifier is None:
        raise ValueError(f"Unknown EMAIL_VERIFIER: {settings.email_verifier}")

    result = await verifier(email)

    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        lead.email_verification_status = result.status
        lead.email_verification_provider = result.provider
        lead.email_verified_at = datetime.utcnow()

    log.info("verify_email_complete", extra={
        "lead_id": lead_id, "status": result.status, "provider": result.provider,
    })
    return result
