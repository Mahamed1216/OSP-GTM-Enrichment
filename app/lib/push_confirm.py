"""Stable confirmation-token logic for the Leads → Push to Instantly flow.

Pure functions (no Streamlit, no DB) so the confirmation behavior is unit
testable. The Leads page stores `build_push_confirm_token(...)` in
`st.session_state` when the operator clicks "Push to Instantly", and on every
rerun compares the stored token to a freshly-computed one.

Why a token instead of a time window: the previous flow gated the
confirmation dialog on a 5-second `time.time()` window. Checking the safety
checkbox triggers a rerun, and if the operator took longer than 5s to read +
check, the window had expired — so the whole dialog (checkbox + Confirm
button) vanished and the push could never be confirmed. A token that only
changes when the *scope* changes (workspace / selected leads / eligible leads
/ campaign) keeps the dialog visible for as long as the operator needs, while
still auto-resetting when any of those inputs change.
"""
from __future__ import annotations

import hashlib
from typing import Iterable


def build_push_confirm_token(
    *,
    workspace_id: int | None,
    selected_ids: Iterable[int],
    eligible_ids: Iterable[int],
    campaign_id: str | None,
) -> str:
    """Stable hash of the confirmation scope.

    Two calls return the same token iff workspace, the *set* of selected lead
    ids, the *set* of eligible lead ids, and the campaign id are all equal.
    Order of ids does not matter (sets are sorted before hashing).
    """
    parts = [
        f"ws={workspace_id}",
        "sel=" + ",".join(str(i) for i in sorted(set(selected_ids))),
        "elig=" + ",".join(str(i) for i in sorted(set(eligible_ids))),
        f"camp={campaign_id or ''}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def confirm_button_enabled(n_eligible: int, ack_checked: bool, hard_limit: int) -> bool:
    """Whether the "Confirm push" button should be clickable.

    Batches over `hard_limit` require the safety checkbox to be checked; at or
    below the limit the Confirm button is the explicit confirmation by itself.
    Always False when there are no eligible leads.
    """
    if n_eligible <= 0:
        return False
    if n_eligible > hard_limit:
        return bool(ack_checked)
    return True


def push_confirmation_active(stored_token: str | None, current_token: str) -> bool:
    """True only when a confirmation is in flight for the CURRENT scope.

    Returns False when nothing is pending (stored_token is None/empty) or when
    the scope changed since the operator clicked Push (stored != current) — the
    caller then clears the stored token and the safety checkbox so the dialog
    resets automatically.
    """
    return bool(stored_token) and stored_token == current_token
