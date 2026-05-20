"""normalize_status() must accept any vendor or cached status string
and return a value the VerifyResult Pydantic Literal accepts.

Regression for ValidationError on cached Apollo status="Verified".
"""
import pytest
from pydantic import ValidationError

from src.delivery.verify_email import VerifyResult, normalize_status


@pytest.mark.parametrize("raw,expected", [
    # Apollo
    ("Verified", "valid"),
    ("verified", "valid"),
    ("Email No Longer Verified", "invalid"),
    # Our own / common vendor variants
    ("Valid", "valid"),
    ("valid", "valid"),
    ("deliverable", "valid"),
    ("ok", "valid"),
    ("OK", "valid"),
    # Risky
    ("Risky", "risky"),
    ("risky", "risky"),
    ("catch_all", "risky"),
    ("accept_all", "risky"),
    ("unknown_catch_all", "risky"),
    ("disposable", "risky"),
    # Invalid
    ("Invalid", "invalid"),
    ("invalid", "invalid"),
    ("bounced", "invalid"),
    ("bad", "invalid"),
    ("undeliverable", "invalid"),
    ("hard_bounce", "invalid"),
    # Unknown / empty / falsy
    (None, "unknown"),
    ("", "unknown"),
    ("Unknown", "unknown"),
    ("something we have never seen", "unknown"),
])
def test_normalize_status_known_inputs(raw, expected):
    assert normalize_status(raw) == expected


def test_normalized_status_constructs_valid_verifyresult():
    """The whole reason normalize_status exists: feed its output straight
    into VerifyResult without Pydantic complaining."""
    for raw in ("Verified", "Email No Longer Verified", "deliverable", "catch_all", None):
        result = VerifyResult(
            status=normalize_status(raw),
            provider="test",
            raw={"raw_status": raw},
        )
        assert result.status in {"valid", "risky", "invalid", "unknown"}


def test_verifyresult_still_rejects_raw_vendor_string_without_normalization():
    """Pin the underlying bug: passing a raw vendor string directly to
    VerifyResult would crash. If this test ever stops raising,
    normalize_status is no longer load-bearing — review carefully."""
    with pytest.raises(ValidationError):
        VerifyResult(status="Verified", provider="apollo", raw={})


def test_cached_apollo_lead_does_not_crash_verify_email_construction():
    """The exact field value the bug report flagged."""
    # Pretend this came from lead.email_verification_status (string from DB).
    raw_db_value = "Verified"
    result = VerifyResult(
        status=normalize_status(raw_db_value),
        provider="Apollo",
        raw={"cached": True, "raw_status": raw_db_value},
    )
    assert result.status == "valid"
    assert result.provider == "Apollo"
