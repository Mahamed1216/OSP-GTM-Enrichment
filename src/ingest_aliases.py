"""Column-header normalization + alias mapping for CSV ingest.

Canonical schema is the existing `Lead` model. This module is the
boundary that lets Apollo / ZoomInfo / Sales Navigator / Lusha exports
land into that schema without the user renaming columns by hand.
"""
from __future__ import annotations

import re

# canonical_field -> list of normalized aliases (the canonical key itself
# must be the first entry so detection picks it as the obvious match)
CANONICAL_ALIASES: dict[str, list[str]] = {
    "first_name": ["first_name", "firstname", "given_name", "fname"],
    "last_name": ["last_name", "lastname", "surname", "lname", "family_name"],
    "email": ["email", "email_address", "work_email", "business_email"],
    "title": ["title", "job_title", "position", "role"],
    "company": [
        "company",
        "company_name",
        "organization",
        "employer",
        "company_name_for_emails",
    ],
    "company_domain": ["company_domain", "website", "domain", "company_website"],
    "industry": ["industry", "company_industry", "sector"],
    "linkedin_url": [
        "linkedin_url",
        "linkedin",
        "linkedin_profile",
        "person_linkedin_url",
        "linkedin_profile_url",
    ],
    "company_linkedin_url": [
        "company_linkedin_url",
        "company_linkedin",
        "organization_linkedin_url",
    ],
}

REQUIRED_FIELDS: list[str] = ["first_name", "last_name", "email"]
OPTIONAL_FIELDS: list[str] = [f for f in CANONICAL_ALIASES if f not in REQUIRED_FIELDS]

_MULTI_UNDERSCORE = re.compile(r"_+")


def normalize_column_name(name: str) -> str:
    """Lowercase, strip, spaces/hyphens -> underscores, collapse repeats."""
    if name is None:
        return ""
    n = str(name).lstrip("﻿").strip().lower()
    n = n.replace("-", "_").replace(" ", "_")
    n = _MULTI_UNDERSCORE.sub("_", n)
    return n.strip("_")


def auto_detect_mapping(csv_columns: list[str]) -> dict[str, str | None]:
    """Return {canonical_field: matched_csv_column or None}.

    First CSV column whose normalized form matches any alias for a
    canonical field wins. Later collisions are ignored (left unmapped at
    the canonical-field level — the surplus CSV column is simply not used).
    """
    normalized_pairs = [(col, normalize_column_name(col)) for col in csv_columns]
    mapping: dict[str, str | None] = {field: None for field in CANONICAL_ALIASES}

    for field, aliases in CANONICAL_ALIASES.items():
        alias_set = set(aliases)
        for original, norm in normalized_pairs:
            if norm in alias_set:
                mapping[field] = original
                break

    return mapping


def validate_mapping(mapping: dict[str, str | None]) -> tuple[bool, list[str]]:
    """Return (is_valid, error_messages). All REQUIRED_FIELDS must be mapped."""
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if not mapping.get(field):
            errors.append(f"Required field '{field}' has no source column mapped.")
    return (not errors, errors)
