"""Bulk delete = loop of single deletes. Verify cascade safety holds
when called repeatedly across multiple leads with full dependent graphs.
"""
from sqlalchemy import func, select

from src.db import session_scope
from src.leads import delete_lead
from src.models import (
    ContentRating,
    Engagement,
    Enrichment,
    GeneratedContent,
    Lead,
    Score,
)


def _make_lead_with_graph(session, *, email: str) -> int:
    """Seed lead + enrichment + score + content + rating + engagement.
    Returns the lead id."""
    lead = Lead(first_name="Bulk", last_name="User", email=email)
    session.add(lead)
    session.flush()

    session.add(Enrichment(lead_id=lead.id, source_status={}))
    session.add(Score(lead_id=lead.id, score=85, tier="A", rationale="r",
                      signals_used=[], model="x"))

    content = GeneratedContent(
        lead_id=lead.id, kind="email", subject="s", body="b",
        prompt_version="v1", model="x",
    )
    session.add(content)
    session.flush()

    session.add(ContentRating(
        generated_content_id=content.id, rating="up", rated_by="demo",
    ))
    session.add(Engagement(content_id=content.id, sent=True, delivered=True))
    session.flush()
    return lead.id


def test_bulk_delete_loop_clears_all_leads_and_dependents():
    with session_scope() as session:
        ids = [
            _make_lead_with_graph(session, email=f"bulk-{i}@example.com")
            for i in range(3)
        ]

    # Sanity: 3 leads, 3 enrichments, 3 scores, 3 contents, 3 ratings, 3 engagements.
    with session_scope() as session:
        assert session.execute(select(func.count(Lead.id))).scalar() == 3
        assert session.execute(select(func.count(Enrichment.id))).scalar() == 3
        assert session.execute(select(func.count(Score.id))).scalar() == 3
        assert session.execute(select(func.count(GeneratedContent.id))).scalar() == 3
        assert session.execute(select(func.count(ContentRating.id))).scalar() == 3
        assert session.execute(select(func.count(Engagement.id))).scalar() == 3

    # The bulk delete is just a loop in the UI — exercise that path.
    summed = {"leads": 0, "enrichments": 0, "scores": 0,
              "generated_contents": 0, "content_ratings": 0, "engagements": 0}
    for lid in ids:
        result = delete_lead(lid)
        assert result["success"], result
        for k, v in result["deleted_counts"].items():
            summed[k] += v

    # All counts sum to 3 across the three deletions.
    assert summed == {"leads": 3, "enrichments": 3, "scores": 3,
                      "generated_contents": 3, "content_ratings": 3, "engagements": 3}

    # And the DB is fully empty across every involved table.
    with session_scope() as session:
        assert session.execute(select(func.count(Lead.id))).scalar() == 0
        assert session.execute(select(func.count(Enrichment.id))).scalar() == 0
        assert session.execute(select(func.count(Score.id))).scalar() == 0
        assert session.execute(select(func.count(GeneratedContent.id))).scalar() == 0
        assert session.execute(select(func.count(ContentRating.id))).scalar() == 0
        assert session.execute(select(func.count(Engagement.id))).scalar() == 0


def test_bulk_delete_continues_past_missing_lead():
    """If one id in the batch doesn't exist, the loop must continue."""
    with session_scope() as session:
        real_id = _make_lead_with_graph(session, email="real@example.com")

    results = [delete_lead(real_id), delete_lead(99999)]
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert results[1]["reason"] == "not found"

    with session_scope() as session:
        assert session.execute(select(func.count(Lead.id))).scalar() == 0
