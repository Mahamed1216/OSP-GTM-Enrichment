"""Scoring schema + tier reconciliation logic."""
import pytest
from pydantic import ValidationError

from src.config import Settings
from src.scoring import ScoreResult


def test_score_schema_rejects_score_above_100():
    with pytest.raises(ValidationError):
        ScoreResult(score=150, tier="A", rationale="x", signals_used=[])


def test_score_schema_rejects_score_below_1():
    with pytest.raises(ValidationError):
        ScoreResult(score=0, tier="C", rationale="x", signals_used=[])


def test_score_schema_rejects_unknown_tier():
    with pytest.raises(ValidationError):
        ScoreResult(score=50, tier="D", rationale="x", signals_used=[])


def test_score_schema_rejects_blank_rationale():
    with pytest.raises(ValidationError):
        ScoreResult(score=80, tier="B", rationale="", signals_used=[])


def test_tier_for_score_at_boundaries():
    s = Settings(tier_a_min=85, tier_b_min=70)
    assert s.tier_for_score(100) == "A"
    assert s.tier_for_score(85) == "A"
    assert s.tier_for_score(84) == "B"
    assert s.tier_for_score(70) == "B"
    assert s.tier_for_score(69) == "C"
    assert s.tier_for_score(1) == "C"


def test_should_send_with_min_b():
    s = Settings(send_min_tier="B")
    assert s.should_send("A") is True
    assert s.should_send("B") is True
    assert s.should_send("C") is False


def test_should_send_with_min_a():
    s = Settings(send_min_tier="A")
    assert s.should_send("A") is True
    assert s.should_send("B") is False
    assert s.should_send("C") is False
