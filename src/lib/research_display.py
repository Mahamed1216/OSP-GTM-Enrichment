"""Pure decision logic for the Lead Detail → Tavily Company Research section.

Kept out of the view layer so the state selection is unit-testable.
Reads the stored buyer_accounts JSON; never triggers any Tavily call.
"""
from __future__ import annotations


def research_display_state(buyer_accounts: dict | None) -> str:
    """Return which Tavily Company Research UI state to render.

    One of:
      - "skipped_disabled" : research skipped because the setting is off
      - "not_run"          : research did not run (e.g. never enriched)
      - "failed"           : research ran but errored
      - "no_useful"        : research completed but no useful company signal
      - "useful"           : research completed with a useful signal
    """
    ba = buyer_accounts or {}
    if (ba.get("research_status") or "") == "skipped_disabled":
        return "skipped_disabled"
    if not ba.get("research_used"):
        return "not_run"
    if (ba.get("research_status") or "") == "failed":
        return "failed"
    if not ba.get("research_useful_signal_found"):
        return "no_useful"
    return "useful"
