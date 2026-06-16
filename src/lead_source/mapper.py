"""Map a ContactOut dict from the external API to local Lead field dict.

ContactOut schema (from openapi.json):
  id, first_name, last_name, title, seniority, department, linkedin_url,
  email, email_verified, email_bounce_risk, email_source, mobile_phone,
  company_name, company_domain, company_industry, company_size, company_naics,
  icp, matched_icps, tier, tier_score, signal_count, signals, source,
  source_metadata, enrichment_status, enrichment_error, is_suppressed,
  suppressed_reason, created_at, updated_at.

Strategy:
  - Map known fields to Lead columns.
  - Store the entire ContactOut as lead_source_raw for provenance.
  - Never crash on missing optional fields — every get() has a default.
  - email_verified=True  →  set email_verification_status="verified" so the
    Push-to-Instantly eligibility check treats this lead as pre-verified.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def map_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """Return a dict of Lead-compatible fields from a ContactOut dict.

    Includes a private key '_full_name' used for dedup-by-domain lookups;
    strip it before passing to Lead() constructor.
    """
    first_name = (contact.get("first_name") or "").strip()
    last_name = (contact.get("last_name") or "").strip()

    email = (contact.get("email") or "").lower().strip()

    company = (contact.get("company_name") or "").strip()
    industry = (contact.get("company_industry") or "").strip()
    linkedin_url = (contact.get("linkedin_url") or "").strip()
    company_domain = (contact.get("company_domain") or "").strip()
    title = (contact.get("title") or "").strip()
    external_id = (contact.get("id") or "").strip()
    # mobile_phone is the primary phone field in ContactOut
    phone = (contact.get("mobile_phone") or contact.get("phone") or "").strip()

    full_name = f"{first_name} {last_name}".strip()

    # Email verification — OSP Lead Engine pre-verifies emails before exposure.
    # When email_verified=True, stamp our local verification columns so the
    # eligibility check (_is_verified in eligibility.py) sees the lead as
    # verified without a separate verification pass.
    email_verified = bool(contact.get("email_verified", False))
    if email_verified:
        email_verification_status = "verified"
        email_verification_provider = "osp_lead_engine"
        email_verified_at: datetime | None = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        email_verification_status = None
        email_verification_provider = None
        email_verified_at = None

    return {
        # Lead model columns
        "external_contact_id": external_id or None,
        "phone": phone or None,
        "first_name": first_name,
        "last_name": last_name,
        "email": email or None,
        "title": title or None,
        "company": company or None,
        "company_domain": company_domain or None,
        "linkedin_url": linkedin_url or None,
        "company_linkedin_url": None,      # not in ContactOut
        "industry": industry or None,
        "lead_source_raw": contact,        # full raw payload — includes signals, matched_icps,
                                           # tier, tier_score, source_metadata, etc.
        # Email verification fields — set from API payload
        "email_verification_status": email_verification_status,
        "email_verification_provider": email_verification_provider,
        "email_verified_at": email_verified_at,
        # Internal dedup helper — NOT a Lead column
        "_full_name": full_name,
    }


def has_identity(fields: dict[str, Any]) -> bool:
    """Return True if the contact has at least one usable identity anchor.

    Priority (same as dedup logic in ingest.py):
      1. email
      2. linkedin_url
      3. company_domain + full_name
    """
    return bool(
        fields.get("email")
        or fields.get("linkedin_url")
        or (fields.get("company_domain") and fields.get("_full_name"))
    )
