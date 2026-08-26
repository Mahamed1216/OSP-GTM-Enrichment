"""Blank environment variables must not take down the backend.

Hosting dashboards save an optional-but-empty variable as ``""`` rather than
omitting it. ``Settings()`` is instantiated at import time in ``src/config.py``,
so every non-str field that cannot parse an empty string used to raise a
ValidationError that propagated through every ``import src.*`` — on Vercel that
took out the whole API:

    ValidationError: 4 validation errors for Settings
    email_verifier  Input should be 'instantly', 'neverbounce' or 'millionverifier'
    tier_a_min      Input should be a valid integer  [input_value='']
    tier_b_min      Input should be a valid integer  [input_value='']
    send_min_tier   Input should be 'A', 'B', 'C' or 'D'  [input_value='']

``env_ignore_empty=True`` makes an empty value mean "unset", so the field
default applies. These tests pass ``_env_file=None`` so a developer's local
.env cannot mask the behaviour under test.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings

# The four fields named in the production error, plus LOG_LEVEL which shares
# the failure mode.
_BLANKABLE = (
    "EMAIL_VERIFIER",
    "TIER_A_MIN",
    "TIER_B_MIN",
    "SEND_MIN_TIER",
    "LOG_LEVEL",
)


def test_all_blank_env_vars_fall_back_to_defaults(monkeypatch):
    for name in _BLANKABLE:
        monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert settings.email_verifier == "instantly"
    assert settings.tier_a_min == 85
    assert settings.tier_b_min == 70
    assert settings.send_min_tier == "B"
    assert settings.log_level == "INFO"


@pytest.mark.parametrize("name", _BLANKABLE)
def test_each_blank_env_var_alone_is_survivable(name, monkeypatch):
    """One blank variable must not break the others."""
    monkeypatch.setenv(name, "")
    assert Settings(_env_file=None) is not None


def test_blank_env_keeps_tiering_behaviour(monkeypatch):
    """The defaults must still produce a working scorer, not just construct."""
    for name in _BLANKABLE:
        monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)

    assert settings.tier_for_score(90) == "A"
    assert settings.tier_for_score(75) == "B"
    assert settings.tier_for_score(10) == "C"
    assert settings.should_send("A") is True
    assert settings.should_send("B") is True
    assert settings.should_send("C") is False
    assert settings.should_send("D") is False


def test_real_values_still_override(monkeypatch):
    """The fix must not stop real configuration from being read."""
    monkeypatch.setenv("TIER_A_MIN", "90")
    monkeypatch.setenv("SEND_MIN_TIER", "A")
    monkeypatch.setenv("EMAIL_VERIFIER", "neverbounce")

    settings = Settings(_env_file=None)

    assert settings.tier_a_min == 90
    assert settings.send_min_tier == "A"
    assert settings.email_verifier == "neverbounce"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TIER_A_MIN", "not-a-number"),
        ("SEND_MIN_TIER", "Z"),
        ("EMAIL_VERIFIER", "nonsense-provider"),
    ],
)
def test_invalid_non_empty_values_still_rejected(name, value, monkeypatch):
    """Validation is intact — only *empty* is treated as unset."""
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_database_url_falls_back_rather_than_crashing(monkeypatch):
    """A blank DATABASE_URL must not raise here.

    api/index.py checks os.environ directly and refuses database-backed routes
    with a clean 503, so the import path only needs to survive.
    """
    monkeypatch.setenv("DATABASE_URL", "")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///sdr.db"
