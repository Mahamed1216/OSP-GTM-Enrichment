"""Phase 5 winners/negatives loader and formatters."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.content import winners as winners_mod
from src.content.winners import (
    NEGATIVE_FRAMING_HEADER,
    WINNER_FRAMING_HEADER,
    format_negatives,
    format_winners_for,
    load_top_negatives,
    load_top_winners_for,
    set_negative_active,
    set_winner_active,
)


@pytest.fixture
def temp_libraries(tmp_path: Path, monkeypatch):
    win_path = tmp_path / "winners.json"
    neg_path = tmp_path / "negatives.json"
    monkeypatch.setattr(winners_mod, "WINNERS_PATH", win_path)
    monkeypatch.setattr(winners_mod, "NEGATIVES_PATH", neg_path)
    return win_path, neg_path


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# load_top_winners_for filters and sorts
# ---------------------------------------------------------------------------

def test_load_top_winners_for_filters_by_content_type(temp_libraries):
    win_path, _ = temp_libraries
    _write(win_path, [
        {"content_type": "email", "score": 0.4, "subject": "e1", "body": "b"},
        {"content_type": "call_script", "score": 0.9, "body": "{}"},
        {"content_type": "email", "score": 0.6, "subject": "e2", "body": "b"},
    ])
    em = load_top_winners_for("email", k=10)
    cs = load_top_winners_for("call_script", k=10)
    assert {w["subject"] for w in em} == {"e1", "e2"}
    assert len(cs) == 1


def test_load_top_winners_for_treats_legacy_entries_as_email(temp_libraries):
    win_path, _ = temp_libraries
    _write(win_path, [
        {"subject": "legacy", "body": "b", "reply_rate": 0.42},  # no content_type
    ])
    out = load_top_winners_for("email", k=10)
    assert len(out) == 1
    assert out[0]["subject"] == "legacy"


def test_load_top_winners_for_sorts_manually_flagged_first_then_score(temp_libraries):
    win_path, _ = temp_libraries
    _write(win_path, [
        {"content_type": "email", "score": 0.9, "manually_flagged": False, "subject": "auto"},
        {"content_type": "email", "score": 0.3, "manually_flagged": True, "subject": "seed"},
    ])
    out = load_top_winners_for("email", k=10)
    assert out[0]["subject"] == "seed"
    assert out[1]["subject"] == "auto"


def test_load_top_winners_for_returns_empty_when_file_missing(temp_libraries):
    out = load_top_winners_for("email", k=3)
    assert out == []


def test_load_top_negatives_filters_and_orders_by_added_at(temp_libraries):
    _, neg_path = temp_libraries
    _write(neg_path, [
        {"content_type": "email", "feedback_reason": "old", "subject": "a",
         "body": "x", "added_at": "2026-01-01T00:00:00Z"},
        {"content_type": "email", "feedback_reason": "new", "subject": "b",
         "body": "y", "added_at": "2026-05-01T00:00:00Z"},
        {"content_type": "call_script", "feedback_reason": "n/a", "body": "{}"},
    ])
    out = load_top_negatives("email", k=2)
    assert [n["subject"] for n in out] == ["b", "a"]


# ---------------------------------------------------------------------------
# format_winners_for / format_negatives — framing & empty-input contracts
# ---------------------------------------------------------------------------

def test_format_winners_for_empty_returns_empty_string():
    assert format_winners_for("email", []) == ""


def test_format_negatives_empty_returns_empty_string():
    """Empty input must collapse the entire section — no orphan headers."""
    assert format_negatives("email", []) == ""


def test_format_negatives_uses_locked_anti_pattern_framing():
    rendered = format_negatives(
        "email",
        [{"feedback_reason": "too generic", "subject": "x", "body": "body",
          "lead_context_summary": "VP of Sales"}],
    )
    assert NEGATIVE_FRAMING_HEADER in rendered
    assert "DO NOT write like these" in rendered
    assert "Avoid the patterns" in rendered
    assert "Why it failed: too generic" in rendered


def test_format_winners_for_uses_locked_positive_framing():
    rendered = format_winners_for(
        "email",
        [{"content_type": "email", "subject": "s", "body": "b",
          "lead_context": {"title": "VP", "industry": "SaaS", "signal": "post"}}],
    )
    assert WINNER_FRAMING_HEADER in rendered
    assert "study these" in rendered
    assert "Subject: s" in rendered
    assert "post" in rendered


def test_format_winners_for_call_script_renders_structured_body():
    body = json.dumps({
        "opener": "perm-based opener",
        "value_prop": "one liner",
        "objections": [{"objection": "no time", "response": "30 sec"}],
        "close": "15 min next week?",
    })
    rendered = format_winners_for(
        "call_script",
        [{"content_type": "call_script", "body": body,
          "lead_context": {"title": "VP", "industry": "SaaS"}}],
    )
    assert "Opener: perm-based opener" in rendered
    assert "Value prop: one liner" in rendered
    assert "no time" in rendered
    assert "Close: 15 min next week?" in rendered


def test_format_winners_for_reads_content_block_or_legacy_topfields(temp_libraries):
    rendered = format_winners_for(
        "email",
        [
            {"content_type": "email",
             "content": {"subject": "from-content", "body": "b1"},
             "lead_context": {"title": "VP", "industry": "SaaS"}},
            {"content_type": "email", "subject": "from-top", "body": "b2",
             "lead_context": {"title": "VP", "industry": "SaaS"}},
        ],
    )
    assert "from-content" in rendered
    assert "from-top" in rendered


# ---------------------------------------------------------------------------
# Phase 7c: is_active filter on the loader + atomic set_*_active helpers
# ---------------------------------------------------------------------------

def test_load_top_winners_for_skips_inactive_entries(temp_libraries):
    win_path, _ = temp_libraries
    _write(win_path, [
        {"id": "a", "content_type": "email", "score": 0.9, "subject": "s", "body": "active",
         "is_active": True},
        {"id": "b", "content_type": "email", "score": 0.8, "subject": "s", "body": "demoted",
         "is_active": False},
    ])
    out = load_top_winners_for("email", k=5)
    ids = [e["id"] for e in out]
    assert "a" in ids
    assert "b" not in ids


def test_load_top_winners_for_treats_missing_is_active_as_active(temp_libraries):
    """Pre-migration entries (no is_active key) must still load."""
    win_path, _ = temp_libraries
    _write(win_path, [
        {"id": "legacy", "content_type": "email", "score": 0.5, "subject": "s", "body": "b"},
    ])
    out = load_top_winners_for("email", k=5)
    assert [e["id"] for e in out] == ["legacy"]


def test_load_top_negatives_skips_inactive_entries(temp_libraries):
    _, neg_path = temp_libraries
    _write(neg_path, [
        {"id": "n1", "content_type": "email", "added_at": "2026-05-10", "body": "active",
         "is_active": True},
        {"id": "n2", "content_type": "email", "added_at": "2026-05-12", "body": "demoted",
         "is_active": False},
    ])
    out = load_top_negatives("email", k=5)
    ids = [e["id"] for e in out]
    assert "n1" in ids
    assert "n2" not in ids


def test_set_winner_active_flips_flag_atomically(temp_libraries):
    win_path, _ = temp_libraries
    _write(win_path, [
        {"id": "a", "content_type": "email", "is_active": True, "subject": "s", "body": "b"},
        {"id": "b", "content_type": "email", "is_active": True, "subject": "s", "body": "b"},
    ])
    changed = set_winner_active("a", False)
    assert changed is True

    on_disk = json.loads(win_path.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in on_disk}
    assert by_id["a"]["is_active"] is False
    assert by_id["b"]["is_active"] is True  # untouched

    # No half-written tmp file lingers.
    leftover = list(win_path.parent.glob("*.tmp"))
    assert leftover == []


def test_set_winner_active_returns_false_when_unknown_or_already_in_state(temp_libraries):
    win_path, _ = temp_libraries
    _write(win_path, [
        {"id": "a", "content_type": "email", "is_active": False, "subject": "s", "body": "b"},
    ])
    # Unknown id.
    assert set_winner_active("does-not-exist", False) is False
    # Already inactive.
    assert set_winner_active("a", False) is False


def test_set_negative_active_writes_to_negatives_file(temp_libraries):
    _, neg_path = temp_libraries
    _write(neg_path, [
        {"id": "n1", "content_type": "email", "is_active": True, "body": "b"},
    ])
    assert set_negative_active("n1", False) is True
    on_disk = json.loads(neg_path.read_text(encoding="utf-8"))
    assert on_disk[0]["is_active"] is False
