"""DATABASE_URL is used as given — the app must never rewrite host or port.

The only transform anywhere in the codebase is `postgres://` -> `postgresql://`
(SQLAlchemy 2.0 has no `postgres` dialect). These tests pin that down, so
"the app is rewriting my pooler URL" can be ruled in or out by running them
rather than by reading code.
"""
from __future__ import annotations

import pytest

from src.db import _normalized_url
from src.db_url import connection_summary, describe_database_url

POOLER = "postgresql://postgres.abcdefghijklmnop:s3cr3t@aws-0-eu-west-2.pooler.supabase.com:6543/postgres"
DIRECT = "postgresql://postgres:s3cr3t@db.cszffzlyurwibqhnetxv.supabase.co:5432/postgres"
DIRECT_POOLER_PORT = "postgresql://postgres:s3cr3t@db.cszffzlyurwibqhnetxv.supabase.co:6543/postgres"


# ---------------------------------------------------------------------------
# The URL is passed through untouched apart from the scheme
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [POOLER, DIRECT, DIRECT_POOLER_PORT])
def test_host_and_port_are_never_rewritten(url):
    assert _normalized_url(url) == url


def test_a_pooler_url_stays_a_pooler_url():
    assert "pooler.supabase.com" in _normalized_url(POOLER)
    assert describe_database_url(POOLER)["database_host"].endswith(
        ".pooler.supabase.com"
    )
    assert describe_database_url(POOLER)["database_uses_pooler"] is True


def test_a_direct_url_stays_a_direct_url():
    assert describe_database_url(DIRECT)["database_host"].startswith("db.")
    assert describe_database_url(DIRECT)["database_uses_pooler"] is False


def test_only_the_scheme_prefix_changes():
    raw = POOLER.replace("postgresql://", "postgres://")
    normalized = _normalized_url(raw)
    assert normalized.startswith("postgresql://")
    # Everything after the scheme is byte-for-byte identical.
    assert normalized.split("://", 1)[1] == raw.split("://", 1)[1]


def test_normalisation_preserves_query_parameters():
    url = POOLER + "?sslmode=require&application_name=signalos"
    assert _normalized_url(url) == url
    assert describe_database_url(url)["database_port"] == 6543


def test_whitespace_is_trimmed_but_nothing_else():
    assert _normalized_url(f"  {POOLER}  ") == POOLER


# ---------------------------------------------------------------------------
# The diagnostic never leaks credentials
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [POOLER, DIRECT, DIRECT_POOLER_PORT])
def test_summary_never_contains_the_password(url):
    assert "s3cr3t" not in str(describe_database_url(url))
    assert "s3cr3t" not in connection_summary(url)


@pytest.mark.parametrize("url", [POOLER, DIRECT])
def test_summary_never_contains_the_username(url):
    summary = str(describe_database_url(url))
    assert "postgres.abcdefghijklmnop" not in summary
    # The shape is reported instead of the value.
    assert describe_database_url(url)["database_user_shape"] in {
        "postgres", "postgres.<project-ref>",
    }


def test_summary_never_contains_the_raw_url():
    assert POOLER not in str(describe_database_url(POOLER))


def test_url_encoded_password_is_not_echoed():
    url = "postgresql://postgres:p%40ssw0rd%21@aws-0-eu.pooler.supabase.com:6543/postgres"
    assert "p%40ssw0rd" not in str(describe_database_url(url))
    assert "p@ssw0rd" not in str(describe_database_url(url))


# ---------------------------------------------------------------------------
# The specific mismatch the deployment reported
# ---------------------------------------------------------------------------

def test_direct_host_with_pooler_port_is_called_out():
    warning = describe_database_url(DIRECT_POOLER_PORT)["database_warning"]
    assert warning
    assert "direct Supabase host with a pooler port" in warning


def test_direct_host_on_its_own_port_warns_about_serverless():
    warning = describe_database_url(DIRECT)["database_warning"]
    assert warning and "Transaction Pooler" in warning


def test_a_correct_pooler_url_raises_no_warning():
    assert describe_database_url(POOLER)["database_warning"] is None


def test_sqlite_is_flagged_as_unsuitable_for_serverless():
    info = describe_database_url("sqlite:///sdr.db")
    assert info["database_scheme"] == "sqlite"
    assert "read-only" in info["database_warning"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", None, "   "])
def test_unset_url_reports_not_configured(value):
    info = describe_database_url(value)
    assert info["database_configured"] is False
    assert info["database_host"] is None


def test_user_shape_of_an_unusual_username():
    info = describe_database_url("postgresql://someoneelse:pw@host:5432/db")
    assert info["database_user_shape"] == "unknown"


def test_connection_summary_is_one_line_of_facts():
    summary = connection_summary(POOLER)
    assert "host=aws-0-eu-west-2.pooler.supabase.com" in summary
    assert "port=6543" in summary
    assert "uses_pooler=True" in summary
