"""Phase 5 learning extension: process_ratings + winners migration."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from src.content import winners as winners_mod
from src.db import session_scope
from src.feedback import learning as learning_mod
from src.feedback.learning import process_ratings
from src.feedback.ratings import record_rating
from src.models import GeneratedContent, Score


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def temp_libraries(tmp_path: Path, monkeypatch):
    win_path = tmp_path / "winners.json"
    neg_path = tmp_path / "negatives.json"
    monkeypatch.setattr(winners_mod, "WINNERS_PATH", win_path)
    monkeypatch.setattr(winners_mod, "NEGATIVES_PATH", neg_path)
    monkeypatch.setattr(learning_mod, "WINNERS_PATH", win_path)
    monkeypatch.setattr(learning_mod, "NEGATIVES_PATH", neg_path)
    return win_path, neg_path


def _make_content(lead_id: int, kind: str = "email", subject: str = "subj") -> int:
    with session_scope() as s:
        c = GeneratedContent(
            lead_id=lead_id, kind=kind, subject=subject,
            body="body", signals_cited=[], prompt_version="v1",
            model="claude-sonnet-4-6",
        )
        s.add(c)
        s.flush()
        return c.id


def _set_score(lead_id: int):
    with session_scope() as s:
        s.add(
            Score(lead_id=lead_id, score=85, tier="A",
                  rationale="r", signals_used=["VP signal"], model="claude-opus-4-7")
        )


# ---------------------------------------------------------------------------
# process_ratings
# ---------------------------------------------------------------------------

def test_process_ratings_thumbs_up_writes_winner(sample_lead_id, temp_libraries):
    _set_score(sample_lead_id)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "up", None))

    summary = process_ratings()
    assert summary["new_winners"] == 1
    assert summary["new_negatives"] == 0

    winners = json.loads(temp_libraries[0].read_text())
    assert len(winners) == 1
    w = winners[0]
    assert w["source"] == "manual_rating"
    assert w["content_type"] == "email"
    assert "rating_id" in w
    assert w["content"]["subject"] == "subj"


def test_process_ratings_thumbs_down_with_feedback_writes_negative(sample_lead_id, temp_libraries):
    _set_score(sample_lead_id)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "down", "too generic"))

    summary = process_ratings()
    assert summary["new_winners"] == 0
    assert summary["new_negatives"] == 1

    negatives = json.loads(temp_libraries[1].read_text())
    assert len(negatives) == 1
    n = negatives[0]
    assert n["source"] == "manual_rating"
    assert n["feedback_reason"] == "too generic"
    assert n["content_type"] == "email"


def test_process_ratings_thumbs_down_without_feedback_skipped(sample_lead_id, temp_libraries):
    _set_score(sample_lead_id)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "down", None))

    summary = process_ratings()
    assert summary["new_negatives"] == 0
    assert summary["skipped_no_feedback"] == 1
    negatives = json.loads(temp_libraries[1].read_text())
    assert negatives == []


def test_process_ratings_is_idempotent(sample_lead_id, temp_libraries):
    _set_score(sample_lead_id)
    cid_up = _make_content(sample_lead_id, subject="up_subj")
    cid_down = _make_content(sample_lead_id, subject="down_subj")
    asyncio.run(record_rating(cid_up, "up", None))
    asyncio.run(record_rating(cid_down, "down", "vague"))

    first = process_ratings()
    second = process_ratings()

    assert first["new_winners"] == 1 and first["new_negatives"] == 1
    assert second["new_winners"] == 0 and second["new_negatives"] == 0
    assert second["winners_total"] == first["winners_total"]
    assert second["negatives_total"] == first["negatives_total"]


def test_process_ratings_preserves_existing_seed_winners(sample_lead_id, temp_libraries):
    """Existing seed entries (no rating_id) must survive process_ratings."""
    win_path = temp_libraries[0]
    win_path.write_text(json.dumps([
        {"id": "seed_001", "content_type": "email", "source": "seed",
         "manually_flagged": True, "score": 0.42, "subject": "seed",
         "body": "b", "lead_context": {"title": "VP", "industry": "SaaS"}},
    ]))
    _set_score(sample_lead_id)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "up", None))

    process_ratings()
    winners = json.loads(win_path.read_text())
    ids = {w.get("id") for w in winners}
    assert "seed_001" in ids
    assert any(w.get("source") == "manual_rating" for w in winners)


# ---------------------------------------------------------------------------
# winning_examples migration script
# ---------------------------------------------------------------------------

def test_winners_migration_adds_new_keys_to_legacy_entries(tmp_path: Path):
    target = tmp_path / "winners.json"
    target.write_text(json.dumps([
        {"id": "seed_001", "subject": "S", "body": "B",
         "manually_flagged": True, "reply_rate": 0.42,
         "lead_context": {"title": "VP", "industry": "SaaS"}},
        {"id": "auto_5", "subject": "S2", "body": "B2",
         "manually_flagged": False, "reply_rate": 1.0,
         "promoted_at": "2026-04-01T00:00:00Z"},
    ]))

    module = _load_module("migrate_winning_examples.py", "migrate_winning_examples")
    summary = module.migrate(target)

    assert summary["migrated"] == 2
    assert summary["wrote_file"] is True

    after = json.loads(target.read_text())
    seed = next(e for e in after if e["id"] == "seed_001")
    auto = next(e for e in after if e["id"] == "auto_5")

    assert seed["content_type"] == "email"
    assert seed["source"] == "seed"
    assert seed["score"] == 0.42
    assert seed["content"] == {"subject": "S", "body": "B"}

    assert auto["source"] == "engagement_reply"
    assert auto["added_at"] == "2026-04-01T00:00:00Z"


def test_winners_migration_is_idempotent(tmp_path: Path):
    target = tmp_path / "winners.json"
    target.write_text(json.dumps([
        {"id": "seed_001", "subject": "S", "body": "B",
         "manually_flagged": True, "reply_rate": 0.42,
         "lead_context": {"title": "VP", "industry": "SaaS"}},
    ]))
    module = _load_module("migrate_winning_examples.py", "migrate_winning_examples_dup")

    first = module.migrate(target)
    second = module.migrate(target)

    assert first["migrated"] == 1
    assert second["migrated"] == 0
    assert second["wrote_file"] is False
    assert second["untouched"] == 1


def test_winners_migration_leaves_already_new_entries_untouched(tmp_path: Path):
    target = tmp_path / "winners.json"
    target.write_text(json.dumps([
        {"id": "rating_3", "content_type": "email", "source": "manual_rating",
         "score": 1.0, "added_at": "2026-05-01T00:00:00Z",
         "content": {"subject": "x", "body": "y"}, "rating_id": 3},
    ]))
    module = _load_module("migrate_winning_examples.py", "migrate_winning_examples_new")
    summary = module.migrate(target)
    assert summary["migrated"] == 0
    assert summary["untouched"] == 1


# ---------------------------------------------------------------------------
# negative_examples bootstrap
# ---------------------------------------------------------------------------

def test_negatives_bootstrap_creates_empty_file_when_missing(tmp_path: Path):
    module = _load_module("migrate_negative_examples.py", "migrate_negative_examples")
    target = tmp_path / "negs.json"
    created = module.ensure(target)
    assert created is True
    assert target.exists()
    assert json.loads(target.read_text()) == []


def test_negatives_bootstrap_no_op_when_present(tmp_path: Path):
    module = _load_module("migrate_negative_examples.py", "migrate_negative_examples_noop")
    target = tmp_path / "negs.json"
    target.write_text("[]")
    created = module.ensure(target)
    assert created is False


# ---------------------------------------------------------------------------
# Phase 7c: new entries from learning helpers must carry is_active=True
# ---------------------------------------------------------------------------

def test_process_ratings_stamps_is_active_true_on_new_winner(sample_lead_id, temp_libraries):
    _set_score(sample_lead_id)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "up", None))
    process_ratings()
    winners = json.loads(temp_libraries[0].read_text())
    assert winners[0]["is_active"] is True


def test_process_ratings_stamps_is_active_true_on_new_negative(sample_lead_id, temp_libraries):
    _set_score(sample_lead_id)
    cid = _make_content(sample_lead_id)
    asyncio.run(record_rating(cid, "down", "off-tone"))
    process_ratings()
    negatives = json.loads(temp_libraries[1].read_text())
    assert negatives[0]["is_active"] is True


def test_promote_winners_make_winner_includes_is_active(sample_lead_id, temp_libraries):
    """_make_winner is called from promote_winners(). Build a fixture
    GeneratedContent + Engagement(replied=True) row and confirm the
    library entry carries is_active=True."""
    from src.feedback.learning import promote_winners
    from src.models import Engagement

    _set_score(sample_lead_id)
    cid = _make_content(sample_lead_id)
    with session_scope() as s:
        s.add(Engagement(content_id=cid, sent=True, delivered=True, replied=True))

    summary = promote_winners()
    assert summary["promoted"] == 1
    winners = json.loads(temp_libraries[0].read_text())
    assert any(w.get("is_active") is True and w["id"] == f"auto_{cid}" for w in winners)
