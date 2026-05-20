"""Tests for src.feedback.ratings."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from src.config import settings
from src.db import session_scope
from src.feedback.ratings import (
    RatingAlreadyExistsError,
    get_rating,
    get_rating_trends,
    get_unrated_content,
    record_rating,
)
from src.models import ContentRating, GeneratedContent, Score


def _make_content(lead_id: int, kind: str, subject: str | None = "subj") -> int:
    with session_scope() as s:
        c = GeneratedContent(
            lead_id=lead_id,
            kind=kind,
            subject=subject,
            body="body",
            signals_cited=[],
            prompt_version="v1",
            model="claude-sonnet-4-6",
        )
        s.add(c)
        s.flush()
        return c.id


def _set_tier(lead_id: int, tier: str, score_value: int = 80) -> None:
    with session_scope() as s:
        s.add(
            Score(
                lead_id=lead_id, score=score_value, tier=tier,
                rationale="r", signals_used=[], model="claude-opus-4-7",
            )
        )


# ============================================================
# record_rating
# ============================================================

@pytest.mark.asyncio
async def test_record_rating_up_happy_path(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    rid = await record_rating(cid, "up", None)
    assert rid > 0
    got = get_rating(cid)
    assert got is not None
    assert got["rating"] == "up"
    assert got["feedback_text"] is None
    assert got["rated_by"] == settings.rater_id


@pytest.mark.asyncio
async def test_record_rating_down_with_feedback_strips_whitespace(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    await record_rating(cid, "down", "  too generic  ")
    got = get_rating(cid)
    assert got["feedback_text"] == "too generic"


@pytest.mark.asyncio
async def test_record_rating_blank_feedback_normalized_to_none(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    await record_rating(cid, "up", "   ")
    got = get_rating(cid)
    assert got["feedback_text"] is None


@pytest.mark.asyncio
async def test_record_rating_invalid_value_raises(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    with pytest.raises(ValueError):
        await record_rating(cid, "meh", None)


@pytest.mark.asyncio
async def test_record_rating_missing_content_raises(sample_lead_id: int):
    with pytest.raises(LookupError):
        await record_rating(99999, "up", None)


@pytest.mark.asyncio
async def test_record_rating_double_rate_raises(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    await record_rating(cid, "up", None)
    with pytest.raises(RatingAlreadyExistsError):
        await record_rating(cid, "down", "changed my mind")


@pytest.mark.asyncio
async def test_record_rating_explicit_rated_by_overrides_default(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    await record_rating(cid, "up", None, rated_by="other_sdr")
    got = get_rating(cid)
    assert got["rated_by"] == "other_sdr"


# ============================================================
# get_rating
# ============================================================

def test_get_rating_returns_none_when_unrated(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    assert get_rating(cid) is None


# ============================================================
# get_unrated_content
# ============================================================

@pytest.mark.asyncio
async def test_get_unrated_excludes_already_rated(sample_lead_id: int):
    rated = _make_content(sample_lead_id, "email")
    unrated = _make_content(sample_lead_id, "email")
    await record_rating(rated, "up", None)
    rows = get_unrated_content()
    ids = {r["id"] for r in rows}
    assert unrated in ids
    assert rated not in ids


def test_get_unrated_excludes_superseded(sample_lead_id: int):
    head = _make_content(sample_lead_id, "email")
    older = _make_content(sample_lead_id, "email")
    with session_scope() as s:
        old_row = s.get(GeneratedContent, older)
        old_row.superseded_by_id = head
    rows = get_unrated_content()
    ids = {r["id"] for r in rows}
    assert head in ids
    assert older not in ids


def test_get_unrated_filters_by_content_type(sample_lead_id: int):
    em = _make_content(sample_lead_id, "email")
    cs = _make_content(sample_lead_id, "call_script")
    rows = get_unrated_content(content_type="email")
    assert {r["id"] for r in rows} == {em}
    rows = get_unrated_content(content_type="call_script")
    assert {r["id"] for r in rows} == {cs}


def test_get_unrated_filters_by_tier(sample_lead_id: int):
    cid = _make_content(sample_lead_id, "email")
    _set_tier(sample_lead_id, "A")
    rows_a = get_unrated_content(tier="A")
    rows_b = get_unrated_content(tier="B")
    assert {r["id"] for r in rows_a} == {cid}
    assert rows_b == []


def test_get_unrated_orders_a_then_b_then_c_then_unscored(sample_lead_id: int):
    """Tier A first, then B, then C, then unscored. Within tier: oldest first."""
    # Three distinct leads with different tiers + one unscored lead
    from src.models import Lead

    def add_lead(email: str) -> int:
        with session_scope() as s:
            l = Lead(first_name="A", last_name="B", email=email)
            s.add(l)
            s.flush()
            return l.id

    lead_a = add_lead("a@x.com"); _set_tier(lead_a, "A")
    lead_b = add_lead("b@x.com"); _set_tier(lead_b, "B")
    lead_c = add_lead("c@x.com"); _set_tier(lead_c, "C")
    lead_u = add_lead("u@x.com")  # no score → unscored

    cid_b = _make_content(lead_b, "email")
    cid_a = _make_content(lead_a, "email")
    cid_u = _make_content(lead_u, "email")
    cid_c = _make_content(lead_c, "email")

    rows = get_unrated_content()
    order = [r["id"] for r in rows]
    # Tier A first, then B, C, then unscored
    assert order.index(cid_a) < order.index(cid_b)
    assert order.index(cid_b) < order.index(cid_c)
    assert order.index(cid_c) < order.index(cid_u)


def test_get_unrated_respects_limit(sample_lead_id: int):
    for _ in range(5):
        _make_content(sample_lead_id, "email")
    rows = get_unrated_content(limit=2)
    assert len(rows) == 2


# ============================================================
# get_rating_trends
# ============================================================

@pytest.mark.asyncio
async def test_get_rating_trends_groups_by_kind_and_date(sample_lead_id: int):
    em = _make_content(sample_lead_id, "email")
    cs = _make_content(sample_lead_id, "call_script")
    await record_rating(em, "up", None)
    await record_rating(cs, "down", "wordy")

    trends = get_rating_trends(days=30)
    assert "email" in trends and "call_script" in trends
    e = trends["email"][0]
    assert e["up_count"] == 1 and e["down_count"] == 0 and e["up_rate"] == 1.0
    c = trends["call_script"][0]
    assert c["up_count"] == 0 and c["down_count"] == 1 and c["up_rate"] == 0.0


@pytest.mark.asyncio
async def test_get_rating_trends_excludes_old_ratings(sample_lead_id: int):
    em = _make_content(sample_lead_id, "email")
    await record_rating(em, "up", None)
    # Backdate the rating beyond the 30-day window
    with session_scope() as s:
        row = s.execute(
            __import__("sqlalchemy").select(ContentRating)
            .where(ContentRating.generated_content_id == em)
        ).scalar_one()
        row.rated_at = datetime.utcnow() - timedelta(days=60)

    assert get_rating_trends(days=30) == {}
    # Wider window picks it up
    assert "email" in get_rating_trends(days=90)
