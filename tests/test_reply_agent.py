"""Tests for the Reply Agent (src/feedback/reply_agent.py).

All tests run without a live LLM — generate_json is monkeypatched or the
pre-validation path is exercised directly (no LLM call needed).
"""
from __future__ import annotations

import asyncio

import pytest

import src.feedback.reply_agent as reply_agent_mod
from src.feedback.reply_agent import (
    ACTION_BOOK_MEETING,
    ACTION_HUMAN,
    ACTION_STOP,
    INTENT_ANGRY,
    INTENT_POSITIVE_INTEREST,
    INTENT_REVSHARE,
    INTENT_UNSUBSCRIBE,
    ReplyAgentResult,
    classify_and_draft_reply,
    save_reply_draft,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_llm(
    classification: str,
    recommended_action: str,
    draft_body: str = "draft body",
    notes: str = "notes",
):
    """Return a generate_json replacement that always returns the given fields."""
    async def _fake(**kwargs):
        return ReplyAgentResult(
            classification=classification,
            recommended_action=recommended_action,
            draft_body=draft_body,
            human_review_notes=notes,
        )
    return _fake


def _capturing_llm(store: dict, result: ReplyAgentResult):
    """Return a generate_json replacement that captures kwargs and returns result."""
    async def _fake(**kwargs):
        store.update(kwargs)
        return result
    return _fake


# ---------------------------------------------------------------------------
# 1. Positive reply creates book_meeting action
# ---------------------------------------------------------------------------

def test_positive_reply_creates_book_meeting(monkeypatch):
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _fake_llm(INTENT_POSITIVE_INTEREST, ACTION_BOOK_MEETING, draft_body="Let's connect."),
    )
    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="I'd love to learn more about your service.",
    ))
    assert result.classification == INTENT_POSITIVE_INTEREST
    assert result.recommended_action == ACTION_BOOK_MEETING


# ---------------------------------------------------------------------------
# 2. Revshare / bounty reply — does not accept terms, asks for a meeting
# ---------------------------------------------------------------------------

def test_revshare_reply_notes_warn_no_accept(monkeypatch):
    # LLM returns revshare classification but without the required warning in notes
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _fake_llm(
            INTENT_REVSHARE,
            ACTION_BOOK_MEETING,
            draft_body="Can we set up a time to discuss?",
            notes="Revshare mentioned.",
        ),
    )
    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="I'm happy to pay you 25% bounty on every one that buys.",
    ))
    assert result.classification == INTENT_REVSHARE
    notes_lower = result.human_review_notes.lower()
    assert "do not accept" in notes_lower or "commercial terms" in notes_lower


def test_revshare_draft_does_not_accept_terms(monkeypatch):
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _fake_llm(
            INTENT_REVSHARE,
            ACTION_BOOK_MEETING,
            draft_body="Can we set up a time to discuss? We'd like to learn more.",
            notes="Commercial terms discussed — do not accept in writing without approval.",
        ),
    )
    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="I'm happy to pay you 25% bounty on every one that buys.",
    ))
    draft_lower = result.draft_body.lower()
    assert "that works" not in draft_lower
    assert "accept the" not in draft_lower
    assert "agreed" not in draft_lower


# ---------------------------------------------------------------------------
# 3. Calendar link is inserted when provided
# ---------------------------------------------------------------------------

def test_calendar_link_forwarded_to_llm(monkeypatch):
    calendar_link = "https://calendly.com/test/30min"
    captured: dict = {}
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _capturing_llm(
            captured,
            ReplyAgentResult(
                classification=INTENT_POSITIVE_INTEREST,
                recommended_action=ACTION_BOOK_MEETING,
                draft_body=f"Book a time here: {calendar_link}",
                human_review_notes="",
            ),
        ),
    )
    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="Happy to discuss further.",
        calendar_link=calendar_link,
    ))
    # Calendar link must appear in the user message sent to the LLM
    assert calendar_link in captured.get("user", ""), "Calendar link must be in LLM user message"
    # And it must survive into the returned draft
    assert calendar_link in result.draft_body


# ---------------------------------------------------------------------------
# 4. No calendar link — fallback message asks to set up a time
# ---------------------------------------------------------------------------

def test_no_calendar_link_prompts_offer_in_user_message(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _capturing_llm(
            captured,
            ReplyAgentResult(
                classification=INTENT_POSITIVE_INTEREST,
                recommended_action=ACTION_BOOK_MEETING,
                draft_body="Happy to set up a time. I can send a calendar link.",
                human_review_notes="",
            ),
        ),
    )
    asyncio.run(classify_and_draft_reply(
        inbound_reply="Sounds interesting.",
        calendar_link="",
    ))
    user_msg = captured.get("user", "").lower()
    assert "none provided" in user_msg or "no calendar" in user_msg or "calendar link" in user_msg


# ---------------------------------------------------------------------------
# 5. Unsubscribe → stop_sequence (no LLM call)
# ---------------------------------------------------------------------------

def test_unsubscribe_phrase_returns_stop_sequence(monkeypatch):
    async def must_not_be_called(**kwargs):
        raise AssertionError("LLM must not be called for unsubscribe")
    monkeypatch.setattr(reply_agent_mod, "generate_json", must_not_be_called)

    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="Please unsubscribe me from this list.",
    ))
    assert result.classification == INTENT_UNSUBSCRIBE
    assert result.recommended_action == ACTION_STOP


def test_remove_me_phrase_returns_stop_sequence(monkeypatch):
    async def must_not_be_called(**kwargs):
        raise AssertionError("LLM must not be called for unsubscribe")
    monkeypatch.setattr(reply_agent_mod, "generate_json", must_not_be_called)

    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="Remove me from your list.",
    ))
    assert result.classification == INTENT_UNSUBSCRIBE
    assert result.recommended_action == ACTION_STOP


def test_stop_emailing_returns_stop_sequence(monkeypatch):
    async def must_not_be_called(**kwargs):
        raise AssertionError("LLM must not be called for unsubscribe")
    monkeypatch.setattr(reply_agent_mod, "generate_json", must_not_be_called)

    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="Stop emailing me.",
    ))
    assert result.classification == INTENT_UNSUBSCRIBE
    assert result.recommended_action == ACTION_STOP


# ---------------------------------------------------------------------------
# 6. Angry complaint → route_to_human
# ---------------------------------------------------------------------------

def test_angry_complaint_routes_to_human(monkeypatch):
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _fake_llm(
            INTENT_ANGRY,
            ACTION_HUMAN,
            draft_body="(Routed to human — do not send.)",
            notes="Angry reply detected.",
        ),
    )
    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="Stop spamming me! I will report this to every authority.",
    ))
    assert result.classification == INTENT_ANGRY
    assert result.recommended_action == ACTION_HUMAN
    assert "do not send" in result.draft_body.lower() or "routed" in result.draft_body.lower()


def test_angry_post_validation_overrides_wrong_action(monkeypatch):
    # LLM mistakenly returns a non-human action for an angry reply
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _fake_llm(INTENT_ANGRY, ACTION_BOOK_MEETING, draft_body="Let's meet.", notes="Angry."),
    )
    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="You are harassing me.",
    ))
    # Post-validation must override to route_to_human
    assert result.recommended_action == ACTION_HUMAN
    assert "do not send" in result.draft_body.lower() or "routed" in result.draft_body.lower()


# ---------------------------------------------------------------------------
# 7. Reply Agent uses selected workspace_id
# ---------------------------------------------------------------------------

def test_workspace_id_accepted_without_error(monkeypatch):
    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _fake_llm(INTENT_POSITIVE_INTEREST, ACTION_BOOK_MEETING),
    )
    result = asyncio.run(classify_and_draft_reply(
        inbound_reply="I'd love to hear more.",
        workspace_id=42,
    ))
    assert result.recommended_action == ACTION_BOOK_MEETING


def test_save_reply_draft_stores_workspace_id():
    result = ReplyAgentResult(
        classification=INTENT_POSITIVE_INTEREST,
        recommended_action=ACTION_BOOK_MEETING,
        draft_body="Let's set up a call.",
        human_review_notes="",
    )
    draft_id = save_reply_draft(
        result,
        inbound_reply="Interested in learning more.",
        workspace_id=1,
    )
    # DB write must succeed (returns a non-None int id)
    assert draft_id is not None
    assert isinstance(draft_id, int)

    from src.db import session_scope
    from src.models import ReplyDraft
    with session_scope() as session:
        row = session.get(ReplyDraft, draft_id)
        assert row is not None
        assert row.workspace_id == 1
        assert row.classification == INTENT_POSITIVE_INTEREST


# ---------------------------------------------------------------------------
# 8. No email is sent automatically
# ---------------------------------------------------------------------------

def test_no_email_sent_automatically(monkeypatch):
    delivery_calls: list = []

    async def fake_push(*args, **kwargs):
        delivery_calls.append((args, kwargs))

    # Patch Instantly push — raises if any delivery call is made
    try:
        import src.delivery.instantly as instantly_mod
        monkeypatch.setattr(instantly_mod, "push_to_instantly", fake_push, raising=False)
    except ImportError:
        pass

    monkeypatch.setattr(
        reply_agent_mod, "generate_json",
        _fake_llm(INTENT_POSITIVE_INTEREST, ACTION_BOOK_MEETING),
    )
    asyncio.run(classify_and_draft_reply(
        inbound_reply="Yes, I'm very interested!",
    ))
    assert delivery_calls == [], "classify_and_draft_reply must not send any emails"
