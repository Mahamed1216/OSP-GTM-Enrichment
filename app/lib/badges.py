"""Markdown badge helpers — tier (A/B/C/D) and generic pills.

`tier_badge()` returns Streamlit `:color-background[...]` markdown for use
inside `st.dataframe` cells (which don't render HTML). `tier_pill_html()`
returns an HTML span styled by the `.tier-pill` rules in app/styles.py —
use this anywhere that accepts `unsafe_allow_html=True`.
"""
from __future__ import annotations

# Mapping for the markdown badge variant (used in dataframes). Note that the
# styles.py colour-background overrides remap green/orange/gray onto the
# editorial tier palette, so these colours are stable across the UI.
_TIER_COLORS: dict[str, str] = {
    "A": "green",
    "B": "orange",
    "C": "gray",
    "D": "gray",
}


def tier_badge(tier: str | None) -> str:
    if not tier:
        return ":gray-background[**—**]"
    color = _TIER_COLORS.get(tier.upper(), "gray")
    return f":{color}-background[**{tier.upper()}**]"


def status_badge(ok: bool) -> str:
    return ":green-background[**OK**]" if ok else ":red-background[**ERR**]"


def pill(text: str, color: str = "blue") -> str:
    return f":{color}-background[{text}]"


_STATUS_PILL_CONFIG: dict[str, tuple[str, str]] = {
    "sent":     ("Sent",     "var(--cobalt)"),
    "replied":  ("Replied",  "var(--color-success)"),
    "opened":   ("Opened",   "var(--color-warning)"),
    "bounced":  ("Bounced",  "var(--color-error)"),
    "pending":  ("Pending",  "var(--ink-muted)"),
}

# Streamlit's dataframe cells render the `:color-background[]` markdown
# shorthand using a fixed colour set; this map picks the closest colour
# token for each delivery state. Used by the Leads table.
STATUS_DATAFRAME_BADGE: dict[str, str] = {
    "replied":  ":green-background[**Replied**]",
    "sent":     ":blue-background[**Sent**]",
    "opened":   ":orange-background[**Opened**]",
    "bounced":  ":red-background[**Bounced**]",
    "pending":  ":gray-background[**Pending**]",
}


def status_pill(state: str) -> str:
    """Inline HTML pill for a delivery state. Use inside contexts that
    accept unsafe_allow_html=True. For st.dataframe cells use
    `STATUS_DATAFRAME_BADGE[state]` (cells don't render arbitrary HTML).
    """
    label, color = _STATUS_PILL_CONFIG.get(
        state, ("—", "var(--hairline-strong)")
    )
    return (
        f'<span class="status-pill" style="background:{color}">'
        f'{label}</span>'
    )


def tier_pill_html(tier: str | None) -> str:
    """HTML tier pill for use inside st.markdown(unsafe_allow_html=True).
    For dataframe cells use `tier_badge()` (cells don't render HTML)."""
    t = (tier or "").upper().strip()
    if t in {"A", "B", "C", "D"}:
        css_class = f"tier-{t.lower()}"
        label = t
    else:
        css_class = "tier-none"
        label = "—"
    return f'<span class="tier-pill {css_class}">{label}</span>'
