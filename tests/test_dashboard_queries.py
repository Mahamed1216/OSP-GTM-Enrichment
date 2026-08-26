"""Phase 5e — DataFrame shape tests for the new dashboard queries."""
from __future__ import annotations

import asyncio

import pytest

from src.lib.db_queries import list_unrated_content, rating_summary_per_content_type
from src.db import session_scope
from src.feedback.ratings import record_rating
from src.models import GeneratedContent, Score


def _make_content(lead_id: int, kind: str = "email", subject: str = "subj") -> int:
    with session_scope() as s:
        c = GeneratedContent(
            lead_id=lead_id, kind=kind, subject=subject, body="body",
            signals_cited=[], prompt_version="v1", model="claude-sonnet-4-6",
        )
        s.add(c)
        s.flush()
        return c.id


def test_list_unrated_content_columns_and_lead_join(sample_lead_id):
    cid = _make_content(sample_lead_id, "email")
    df = list_unrated_content()
    assert list(df.columns) == ["id", "lead_id", "Lead", "Company", "Type", "Tier", "Created"]
    assert len(df) == 1
    row = df.iloc[0]
    assert row["id"] == cid
    assert row["lead_id"] == sample_lead_id
    assert row["Lead"] == "Test User"
    assert row["Company"] == "ExampleCo"
    assert row["Type"] == "Email"


def test_list_unrated_content_returns_empty_frame_with_columns_when_no_rows():
    df = list_unrated_content()
    assert df.empty
    assert list(df.columns) == ["id", "lead_id", "Lead", "Company", "Type", "Tier", "Created"]


def test_list_unrated_content_includes_tier_when_scored(sample_lead_id):
    _make_content(sample_lead_id, "email")
    with session_scope() as s:
        s.add(Score(
            lead_id=sample_lead_id, score=85, tier="A",
            rationale="r", signals_used=[], model="claude-opus-4-7",
        ))
    df = list_unrated_content()
    assert df.iloc[0]["Tier"] == "A"


def test_rating_summary_pivots_kind_and_date(sample_lead_id):
    em = _make_content(sample_lead_id, "email")
    cs = _make_content(sample_lead_id, "call_script")
    asyncio.run(record_rating(em, "up", None))
    asyncio.run(record_rating(cs, "down", "wordy"))

    df = rating_summary_per_content_type(days=30)
    assert not df.empty
    # Pivoted: index is date, columns are friendly content-type labels
    assert "Email" in df.columns
    assert "Call Script" in df.columns
    # Email had 1 up / 0 down → up_rate 1.0; Call Script had 0 up / 1 down → 0.0
    only_row = df.iloc[0]
    assert only_row["Email"] == 1.0
    assert only_row["Call Script"] == 0.0


def test_rating_summary_empty_when_no_ratings():
    df = rating_summary_per_content_type(days=30)
    assert df.empty
