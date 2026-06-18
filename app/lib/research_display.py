"""Pure decision logic for the Lead Detail → Tavily Company Research section.

Kept out of the Streamlit page so the state selection is unit-testable.
Reads the stored buyer_accounts JSON; never triggers any Tavily call.
"""
from __future__ import annotations


def research_display_state(buyer_accounts: dict | None) -> str:
    """Return which Tavily Company Research UI state to render.

    One of:
      - "not_run"   : research did not run (disabled / never enriched)
      - "failed"    : research ran but errored
      - "no_useful" : research completed but no useful company signal found
      - "useful"    : research completed with a useful signal
    """
    ba = buyer_accounts or {}
    if not ba.get("research_used"):
        return "not_run"
    if (ba.get("research_status") or "") == "failed":
        return "failed"
    if not ba.get("research_useful_signal_found"):
        return "no_useful"
    return "useful"
