"""Content generation: schema enforcement, JSON parsing, context formatting."""
import pytest
from pydantic import ValidationError

from src.content.email import EmailResult
from src.content.winners import format_winners
from src.context import format_lead_context
from src.llm import _extract_first_json_block
from src.models import Lead, Score


def test_email_schema_accepts_valid():
    e = EmailResult(subject="Hi", body="Body text", signals_cited=["recent post"])
    assert e.subject == "Hi"
    assert e.signals_cited == ["recent post"]


def test_email_schema_rejects_blank_body():
    with pytest.raises(ValidationError):
        EmailResult(subject="Hi", body="", signals_cited=["x"])


def test_email_schema_rejects_blank_subject():
    with pytest.raises(ValidationError):
        EmailResult(subject="", body="Body", signals_cited=["x"])


def test_extract_json_handles_fenced_block():
    assert _extract_first_json_block('```json\n{"a":1}\n```') == '{"a":1}'


def test_extract_json_handles_prose_prefix():
    assert _extract_first_json_block('Here is the JSON: {"a":1, "b":2}') == '{"a":1, "b":2}'


def test_extract_json_handles_brace_in_string():
    raw = '{"key": "}", "n": 1}'
    assert _extract_first_json_block(raw) == raw


def test_extract_json_handles_array():
    assert _extract_first_json_block('[1, 2, 3]') == '[1, 2, 3]'


def test_format_lead_context_includes_score_when_present():
    lead = Lead(
        first_name="A", last_name="B", email="a@b.com",
        title="VP", company="X", industry="SaaS",
    )
    score = Score(score=85, tier="A", rationale="strong fit", signals_used=["s1"], model="opus")
    out = format_lead_context(lead, None, score)
    assert "85" in out
    assert "(A)" in out
    assert "strong fit" in out
    assert "s1" in out


def test_format_lead_context_omits_score_when_disabled():
    lead = Lead(first_name="A", last_name="B", email="a@b.com", title="VP", company="X")
    score = Score(score=85, tier="A", rationale="r", signals_used=[], model="opus")
    out = format_lead_context(lead, None, score, include_score=False)
    assert "85" not in out
    assert "Score" not in out


def test_format_winners_includes_signal_in_output():
    winners = [{
        "lead_context": {"title": "VP", "industry": "SaaS", "signal": "specific signal X"},
        "subject": "subj", "body": "body content",
    }]
    out = format_winners(winners)
    assert "specific signal X" in out
    assert "subj" in out
    assert "body content" in out


def test_format_winners_returns_empty_for_empty_list():
    assert format_winners([]) == ""
