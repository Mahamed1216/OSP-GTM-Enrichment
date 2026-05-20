"""CSV column normalization, alias mapping, and validation.

Covers:
- Snake-case headers (data/sample_leads.csv shape) — regression: no mapping
  friction, validate_mapping passes immediately.
- Apollo headers from the spec's Verification list — all canonical fields
  the export covers must auto-detect.
- ZoomInfo-style headers — common variants land on canonical fields.
- Case / whitespace / hyphen tolerance: FIRST NAME, first name, First_Name,
  FIRST-NAME all map to first_name.
- Missing required field — validate_mapping returns (False, [...]).
- Unknown columns — silently ignored, no crash.
- Duplicate-match tie-breaker — first CSV column wins for that canonical
  field; later collisions stay unmapped at the canonical level.
"""
from src.ingest_aliases import (
    CANONICAL_ALIASES,
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    auto_detect_mapping,
    normalize_column_name,
    validate_mapping,
)

# Exact column set from the spec's Verification section (Apollo export)
APOLLO_HEADERS = [
    "First Name",
    "Last Name",
    "Title",
    "Company Name",
    "Email",
    "Email Status",
    "Primary Email Source",
    "Person Linkedin Url",
    "Company Linkedin Url",
    "Industry",
    "Website",
    "Company Name for Emails",  # collision with Company Name -> company
]

# A representative ZoomInfo-style header set
ZOOMINFO_HEADERS = [
    "First Name",
    "Last Name",
    "Email Address",
    "Job Title",
    "Company",
    "Company Website",
    "Company Industry",
    "LinkedIn Profile URL",
]

SNAKE_HEADERS = [
    "first_name",
    "last_name",
    "email",
    "title",
    "company",
    "company_domain",
    "linkedin_url",
    "company_linkedin_url",
    "industry",
]


# ---------------------------- normalize ----------------------------------

def test_normalize_lowercases_and_strips():
    assert normalize_column_name("  First Name  ") == "first_name"


def test_normalize_spaces_and_hyphens_to_underscores():
    assert normalize_column_name("First-Name") == "first_name"
    assert normalize_column_name("FIRST NAME") == "first_name"
    assert normalize_column_name("First_Name") == "first_name"
    assert normalize_column_name("FIRST-NAME") == "first_name"


def test_normalize_collapses_repeated_underscores():
    assert normalize_column_name("first  name") == "first_name"
    assert normalize_column_name("first - name") == "first_name"


def test_normalize_handles_none_and_empty():
    assert normalize_column_name("") == ""
    assert normalize_column_name(None) == ""


def test_normalize_strips_utf8_bom():
    """Apollo / Excel exports often start with a UTF-8 BOM glued onto the
    first column header. csv module + utf-8-sig handle this at the file
    level, but be defensive in the normalizer too."""
    assert normalize_column_name("﻿First Name") == "first_name"


# ---------------------------- snake_case regression ----------------------

def test_snake_case_headers_map_one_to_one():
    """sample_leads.csv keeps working: every header is its own canonical name."""
    mapping = auto_detect_mapping(SNAKE_HEADERS)
    for field in SNAKE_HEADERS:
        assert mapping[field] == field, f"{field} should self-map"
    ok, errors = validate_mapping(mapping)
    assert ok, errors


# ---------------------------- Apollo --------------------------------------

def test_apollo_headers_auto_detect_all_canonical_fields_present_in_export():
    mapping = auto_detect_mapping(APOLLO_HEADERS)
    assert mapping["first_name"] == "First Name"
    assert mapping["last_name"] == "Last Name"
    assert mapping["email"] == "Email"
    assert mapping["title"] == "Title"
    assert mapping["company"] == "Company Name"  # first-match wins
    assert mapping["company_domain"] == "Website"
    assert mapping["industry"] == "Industry"
    assert mapping["linkedin_url"] == "Person Linkedin Url"
    assert mapping["company_linkedin_url"] == "Company Linkedin Url"


def test_apollo_validation_passes():
    ok, errors = validate_mapping(auto_detect_mapping(APOLLO_HEADERS))
    assert ok, errors


def test_apollo_collision_first_match_wins():
    """Both 'Company Name' and 'Company Name for Emails' alias to company;
    the first one in the CSV header order wins."""
    mapping = auto_detect_mapping(APOLLO_HEADERS)
    assert mapping["company"] == "Company Name"


# ---------------------------- ZoomInfo ------------------------------------

def test_zoominfo_headers_auto_detect():
    mapping = auto_detect_mapping(ZOOMINFO_HEADERS)
    assert mapping["first_name"] == "First Name"
    assert mapping["last_name"] == "Last Name"
    assert mapping["email"] == "Email Address"
    assert mapping["title"] == "Job Title"
    assert mapping["company"] == "Company"
    assert mapping["company_domain"] == "Company Website"
    assert mapping["industry"] == "Company Industry"
    assert mapping["linkedin_url"] == "LinkedIn Profile URL"
    ok, errors = validate_mapping(mapping)
    assert ok, errors


# ---------------------------- validation ----------------------------------

def test_missing_required_field_fails_validation():
    headers = ["First Name", "Email"]  # no last_name
    ok, errors = validate_mapping(auto_detect_mapping(headers))
    assert not ok
    assert any("last_name" in e for e in errors)


def test_missing_all_required_fields_reports_each():
    ok, errors = validate_mapping(auto_detect_mapping(["foo", "bar"]))
    assert not ok
    assert len(errors) == len(REQUIRED_FIELDS)


# ---------------------------- unknown columns -----------------------------

def test_unknown_columns_are_silently_unmapped():
    headers = ["first_name", "last_name", "email", "Mystery Column", "Stage"]
    mapping = auto_detect_mapping(headers)
    # required fields still mapped
    assert mapping["first_name"] == "first_name"
    assert mapping["last_name"] == "last_name"
    assert mapping["email"] == "email"
    # unknown columns don't appear as values anywhere in the mapping
    assert "Mystery Column" not in mapping.values()
    assert "Stage" not in mapping.values()


def test_optional_fields_absent_in_csv_stay_none():
    headers = ["first_name", "last_name", "email"]
    mapping = auto_detect_mapping(headers)
    for field in OPTIONAL_FIELDS:
        assert mapping[field] is None


# ---------------------------- case variations -----------------------------

def test_case_and_separator_variants_all_match():
    for header in ["FIRST NAME", "first name", "First_Name", "FIRST-NAME", "first-name"]:
        mapping = auto_detect_mapping([header, "Last Name", "Email"])
        assert mapping["first_name"] == header, f"{header!r} did not match"


# ---------------------------- shape sanity --------------------------------

def test_mapping_always_contains_every_canonical_key():
    mapping = auto_detect_mapping([])
    assert set(mapping.keys()) == set(CANONICAL_ALIASES.keys())
    for v in mapping.values():
        assert v is None
