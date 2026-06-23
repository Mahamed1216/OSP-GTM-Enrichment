"""Push-to-Instantly confirmation flow (hotfix).

The confirmation now uses a STABLE scope token instead of a 5-second time
window, so checking the safety checkbox (which reruns Streamlit) can never make
the dialog disappear. These tests cover the pure token/gate logic plus the
server-side eligibility safety that runs again on confirm.
"""
from __future__ import annotations

from sqlalchemy import select

from app.lib.push_confirm import (
    build_push_confirm_token,
    confirm_button_enabled,
    push_confirmation_active,
)
from src.db import session_scope
from src.delivery.eligibility import filter_eligible
from src.models import GeneratedContent, Lead, Score, now_utc
from src.workspace import get_default_workspace_id, seed_default_workspace


def _token(ws, selected, eligible, campaign="camp-1"):
    return build_push_confirm_token(
        workspace_id=ws, selected_ids=selected, eligible_ids=eligible, campaign_id=campaign
    )


# ---------------------------------------------------------------------------
# 1. Confirmation persists across rerun for the same selected eligible leads
# ---------------------------------------------------------------------------

def test_token_stable_for_same_scope():
    a = _token(1, [3, 1, 2], [1, 2])
    b = _token(1, [1, 2, 3], [2, 1])  # same sets, different order
    assert a == b
    assert push_confirmation_active(a, b) is True


def test_confirmation_active_only_when_token_matches():
    t = _token(1, [1, 2], [1, 2])
    assert push_confirmation_active(t, t) is True
    assert push_confirmation_active(None, t) is False
    assert push_confirmation_active("", t) is False
    assert push_confirmation_active("stale", t) is False


# ---------------------------------------------------------------------------
# 2. Confirmation clears when selected lead IDs change
# ---------------------------------------------------------------------------

def test_token_changes_when_selection_changes():
    before = _token(1, [1, 2], [1, 2])
    after = _token(1, [1, 2, 3], [1, 2, 3])
    assert before != after
    assert push_confirmation_active(before, after) is False


# ---------------------------------------------------------------------------
# 3. Confirmation clears when workspace changes
# ---------------------------------------------------------------------------

def test_token_changes_when_workspace_changes():
    a = _token(1, [1, 2], [1, 2])
    b = _token(2, [1, 2], [1, 2])
    assert a != b
    assert push_confirmation_active(a, b) is False


def test_token_changes_when_eligibility_changes():
    a = _token(1, [1, 2, 3], [1, 2, 3])
    b = _token(1, [1, 2, 3], [1, 2])  # one became ineligible
    assert a != b


def test_token_changes_when_campaign_changes():
    a = _token(1, [1], [1], campaign="camp-1")
    b = _token(1, [1], [1], campaign="camp-2")
    assert a != b


# ---------------------------------------------------------------------------
# 4 & 5. Confirm button gate: blocked when checkbox false, enabled when true
# ---------------------------------------------------------------------------

def test_confirm_blocked_when_checkbox_false_for_big_batch():
    # Over the hard limit and unchecked → disabled.
    assert confirm_button_enabled(50, ack_checked=False, hard_limit=10) is False


def test_confirm_enabled_when_checkbox_true_for_big_batch():
    assert confirm_button_enabled(50, ack_checked=True, hard_limit=10) is True


def test_confirm_enabled_small_batch_without_checkbox():
    # At/below the limit, the Confirm button itself is the confirmation.
    assert confirm_button_enabled(5, ack_checked=False, hard_limit=10) is True


def test_confirm_disabled_when_no_eligible():
    assert confirm_button_enabled(0, ack_checked=True, hard_limit=10) is False


# ---------------------------------------------------------------------------
# 8 & 9. Server-side filter: only selected eligible leads, no unselected leak
# ---------------------------------------------------------------------------

def _seed_ws() -> int:
    seed_default_workspace()
    ws = get_default_workspace_id()
    assert ws is not None
    return ws


def _sendable_lead(ws: int, email: str, *, body: str = "Hi there, real body.") -> int:
    """Create a fully-eligible lead (verified, A-tier, valid content)."""
    with session_scope() as s:
        lead = Lead(
            first_name="T", last_name="U", email=email, workspace_id=ws,
            email_verification_status="verified",
            email_verification_provider="osp_lead_engine",
            email_verified_at=now_utc(),
        )
        s.add(lead)
        s.flush()
        lid = lead.id
        s.add(Score(lead_id=lid, score=95, tier="A", rationale="x",
                    signals_used=[], model="t", workspace_id=ws))
        s.add(GeneratedContent(
            lead_id=lid, kind="email", subject="hi", body=body,
            signals_cited=[], prompt_version="v", model="m", workspace_id=ws,
        ))
        return lid


def test_only_selected_eligible_are_pushed_and_no_unselected_leak():
    ws = _seed_ws()
    a = _sendable_lead(ws, "a@x.com")
    b = _sendable_lead(ws, "b@x.com")
    unselected = _sendable_lead(ws, "c@x.com")  # eligible but NOT selected

    selected = [a, b]
    with session_scope() as s:
        eligible, _skipped = filter_eligible(selected, s)

    # Eligible is a subset of the selection — the unselected lead never appears.
    assert set(eligible) <= set(selected)
    assert unselected not in eligible
    assert set(eligible) == {a, b}


# ---------------------------------------------------------------------------
# 6 & 7. Unsafe internal review content and missing content stay blocked
# ---------------------------------------------------------------------------

def test_unsafe_and_missing_content_excluded_by_filter():
    ws = _seed_ws()
    good = _sendable_lead(ws, "good@x.com")
    unsafe = _sendable_lead(ws, "unsafe@x.com",
                            body="NEEDS REVIEW: No direct buyer account found.")
    empty = _sendable_lead(ws, "empty@x.com", body="   ")

    with session_scope() as s:
        eligible, skipped = filter_eligible([good, unsafe, empty], s)

    assert good in eligible
    assert unsafe in skipped["unsafe_content"]
    assert empty in skipped["no_content"]
    assert unsafe not in eligible
    assert empty not in eligible
