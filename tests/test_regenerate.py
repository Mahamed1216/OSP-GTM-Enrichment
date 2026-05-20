"""Phase 5d — regenerate_with_feedback. Generators monkeypatched (no live API)."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from src.db import session_scope
from src.feedback import regenerate as regenerate_mod
from src.feedback.ratings import record_rating
from src.feedback.regenerate import RegenerateRefused, regenerate_with_feedback
from src.models import GeneratedContent


def _make_content(lead_id: int, kind: str = "email", subject: str = "subj") -> int:
    with session_scope() as s:
        c = GeneratedContent(
            lead_id=lead_id, kind=kind, subject=subject, body="orig body",
            signals_cited=[], prompt_version="v1", model="claude-sonnet-4-6",
        )
        s.add(c)
        s.flush()
        return c.id


def _patch_email_generator(monkeypatch):
    """Replace generate_email with a fake that inserts a row and returns dummy result."""
    captured: dict = {}

    async def _fake(lead_id: int, *, regeneration_feedback: str | None = None):
        captured["lead_id"] = lead_id
        captured["regeneration_feedback"] = regeneration_feedback
        with session_scope() as s:
            new = GeneratedContent(
                lead_id=lead_id, kind="email",
                subject="REGENERATED",
                body=f"new body ({regeneration_feedback or ''})",
                signals_cited=[], prompt_version="v1", model="claude-sonnet-4-6",
            )
            s.add(new)
            s.flush()

        class _R:
            subject = "REGENERATED"
            body = f"new body ({regeneration_feedback or ''})"
            signals_cited: list = []
        return _R()

    monkeypatch.setattr(regenerate_mod, "_KIND_DISPATCH", {**regenerate_mod._KIND_DISPATCH, "email": _fake})
    return captured


# ============================================================
# Happy path
# ============================================================

def test_regenerate_happy_path_creates_new_row_and_supersedes_old(sample_lead_id, monkeypatch):
    captured = _patch_email_generator(monkeypatch)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "down", "too generic"))

    new_id = asyncio.run(regenerate_with_feedback(cid))

    assert new_id != cid
    assert captured["regeneration_feedback"] == "too generic"

    with session_scope() as s:
        old = s.get(GeneratedContent, cid)
        new = s.get(GeneratedContent, new_id)
        assert old.superseded_by_id == new_id
        assert new.superseded_by_id is None
        assert new.subject == "REGENERATED"
        assert "too generic" in new.body


# ============================================================
# Refusals
# ============================================================

def test_regenerate_refuses_already_superseded(sample_lead_id, monkeypatch):
    _patch_email_generator(monkeypatch)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "down", "tag"))
    new_id = asyncio.run(regenerate_with_feedback(cid))

    # Now try to regenerate the already-superseded original — must refuse.
    with pytest.raises(RegenerateRefused) as exc:
        asyncio.run(regenerate_with_feedback(cid))
    assert "already superseded" in str(exc.value)


def test_regenerate_refuses_when_no_rating(sample_lead_id, monkeypatch):
    _patch_email_generator(monkeypatch)
    cid = _make_content(sample_lead_id)
    with pytest.raises(RegenerateRefused) as exc:
        asyncio.run(regenerate_with_feedback(cid))
    assert "no rating" in str(exc.value)


def test_regenerate_refuses_thumbs_up(sample_lead_id, monkeypatch):
    _patch_email_generator(monkeypatch)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "up", "great"))
    with pytest.raises(RegenerateRefused) as exc:
        asyncio.run(regenerate_with_feedback(cid))
    assert "thumbs-down" in str(exc.value).lower() or "rating=" in str(exc.value)


def test_regenerate_refuses_down_without_feedback(sample_lead_id, monkeypatch):
    _patch_email_generator(monkeypatch)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "down", None))
    with pytest.raises(RegenerateRefused) as exc:
        asyncio.run(regenerate_with_feedback(cid))
    assert "feedback" in str(exc.value).lower()


def test_regenerate_refuses_missing_content():
    with pytest.raises(LookupError):
        asyncio.run(regenerate_with_feedback(99999))
