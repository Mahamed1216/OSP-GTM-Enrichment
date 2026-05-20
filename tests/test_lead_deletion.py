"""delete_lead must remove a lead and every row that references it.

Coverage:
- Happy path: enrichment, score, two content rows (with a supersede
  chain), rating, engagement all gone; counts reported correctly.
- Idempotent: deleting a missing lead returns success=False without
  raising.
- Supersede chain: self-FK doesn't block the delete even with PRAGMA
  foreign_keys=ON (SQLite normally has FKs off, so we explicitly turn
  them on for this test).
- Engagement cleanup: explicit DELETE on Engagement (no SQLAlchemy
  cascade) actually fires.
- Isolation: other leads survive untouched.
"""
from sqlalchemy import select, text

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


def _make_lead(session, *, email: str, first: str = "Test") -> Lead:
    lead = Lead(
        first_name=first,
        last_name="User",
        email=email,
        title="VP of Sales",
        company="ExampleCo",
        company_domain="example.com",
        industry="B2B SaaS",
        linkedin_url="https://www.linkedin.com/in/test-user",
        company_linkedin_url="https://www.linkedin.com/company/example",
    )
    session.add(lead)
    session.flush()
    return lead


def _build_full_graph(lead_id: int) -> dict[str, int]:
    """Attach a complete dependent graph to the given lead. Returns ids."""
    with session_scope() as session:
        session.add(Enrichment(lead_id=lead_id, source_status={}))

        session.add(
            Score(
                lead_id=lead_id,
                score=85,
                tier="A",
                rationale="strong fit",
                signals_used=["title"],
                model="claude-opus-4-7",
            )
        )

        # Two content rows: B supersedes A (A.superseded_by_id = B.id).
        a = GeneratedContent(
            lead_id=lead_id,
            kind="email",
            subject="v1 subject",
            body="v1 body",
            prompt_version="v1",
            model="claude-opus-4-7",
        )
        b = GeneratedContent(
            lead_id=lead_id,
            kind="email",
            subject="v2 subject",
            body="v2 body",
            prompt_version="v2",
            model="claude-opus-4-7",
        )
        session.add_all([a, b])
        session.flush()
        a.superseded_by_id = b.id

        session.add(
            ContentRating(
                generated_content_id=a.id,
                rating="down",
                feedback_text="too generic",
                rated_by="tester",
            )
        )

        session.add(
            Engagement(
                content_id=b.id,
                sent=True,
                delivered=True,
                opened=True,
            )
        )

        return {"a_id": a.id, "b_id": b.id}


def test_delete_lead_removes_all_dependent_rows(sample_lead_id):
    _build_full_graph(sample_lead_id)

    result = delete_lead(sample_lead_id)

    assert result["success"] is True
    assert result["lead_id"] == sample_lead_id
    assert result["deleted_counts"] == {
        "leads": 1,
        "enrichments": 1,
        "scores": 1,
        "generated_contents": 2,
        "content_ratings": 1,
        "engagements": 1,
    }

    with session_scope() as session:
        assert session.get(Lead, sample_lead_id) is None
        assert session.execute(
            select(Enrichment).where(Enrichment.lead_id == sample_lead_id)
        ).scalar_one_or_none() is None
        assert session.execute(
            select(Score).where(Score.lead_id == sample_lead_id)
        ).scalar_one_or_none() is None
        assert session.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == sample_lead_id)
        ).all() == []
        # ContentRating + Engagement have no lead_id FK; check via content link.
        assert session.execute(select(ContentRating)).all() == []
        assert session.execute(select(Engagement)).all() == []


def test_delete_lead_idempotent_when_missing():
    result = delete_lead(99_999)
    assert result == {
        "success": False,
        "lead_id": 99_999,
        "reason": "not found",
    }

    # Second call still doesn't raise.
    result2 = delete_lead(99_999)
    assert result2["success"] is False


def test_delete_lead_clears_supersede_chain_under_fk_enforcement(sample_lead_id):
    """Force SQLite to enforce FKs (off by default) and confirm the
    self-referencing GeneratedContent.superseded_by_id chain doesn't trip
    the cascade.

    Tests use a shared StaticPool connection (conftest), so we set the
    pragma on that single connection rather than disposing the engine —
    disposing would lose the in-memory schema.
    """
    from src import db as db_module

    with db_module.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    try:
        with session_scope() as session:
            assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1

        ids = _build_full_graph(sample_lead_id)

        result = delete_lead(sample_lead_id)
        assert result["success"] is True

        with session_scope() as session:
            remaining = session.execute(
                select(GeneratedContent).where(
                    GeneratedContent.id.in_([ids["a_id"], ids["b_id"]])
                )
            ).all()
            assert remaining == []
    finally:
        with db_module.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys = OFF")


def test_delete_lead_removes_engagements_without_cascade(sample_lead_id):
    """Engagement has no SQLAlchemy cascade; the explicit DELETE in
    delete_lead is what cleans it up. Regression for the easy bug of
    relying on a cascade that doesn't exist.
    """
    with session_scope() as session:
        content = GeneratedContent(
            lead_id=sample_lead_id,
            kind="email",
            subject="s",
            body="b",
            prompt_version="v1",
            model="claude-opus-4-7",
        )
        session.add(content)
        session.flush()
        session.add(Engagement(content_id=content.id, sent=True))

    result = delete_lead(sample_lead_id)
    assert result["success"] is True
    assert result["deleted_counts"]["engagements"] == 1
    assert result["deleted_counts"]["generated_contents"] == 1

    with session_scope() as session:
        assert session.execute(select(Engagement)).all() == []
        assert session.execute(select(GeneratedContent)).all() == []


def test_delete_lead_preserves_other_leads(sample_lead_id):
    with session_scope() as session:
        other = _make_lead(session, email="other@example.com", first="Other")
        other_id = other.id

    _build_full_graph(other_id)

    result = delete_lead(sample_lead_id)
    assert result["success"] is True

    with session_scope() as session:
        survivor = session.get(Lead, other_id)
        assert survivor is not None
        assert survivor.email == "other@example.com"
        # Its dependent rows are intact.
        assert (
            session.execute(
                select(Enrichment).where(Enrichment.lead_id == other_id)
            ).scalar_one_or_none()
            is not None
        )
        assert (
            session.execute(
                select(Score).where(Score.lead_id == other_id)
            ).scalar_one_or_none()
            is not None
        )
        contents = session.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == other_id)
        ).all()
        assert len(contents) == 2
