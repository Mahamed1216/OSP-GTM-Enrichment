"""Markdown badge helpers — tier and delivery-state pills for dataframes.

Streamlit's dataframe cells DON'T render arbitrary HTML, but they DO
render the built-in `:color-background[**text**]` markdown shorthand
when the cell sits in an auto-typed string column. `tier_badge()` and
`status_pill()` return those shorthand strings so the Leads table can
display colored pills with no HTML escape hatch.

For non-dataframe contexts where HTML is acceptable (e.g. inside
`st.markdown(..., unsafe_allow_html=True)`), see `tier_pill_html()`.
"""
from __future__ import annotations

_TIER_BADGE: dict[str, str] = {
    "A": ":blue-background[**A**]",
    "B": ":orange-background[**B**]",
    "C": ":gray-background[**C**]",
    "D": ":gray-background[**D**]",
}

_STATUS_BADGE: dict[str, str] = {
    "sent":    ":blue-background[**Sent**]",
    "replied": ":green-background[**Replied**]",
    "opened":  ":orange-background[**Opened**]",
    "bounced": ":red-background[**Bounced**]",
    "pending": ":gray-background[**Pending**]",
}


def tier_badge(tier: str | None) -> str:
    """Streamlit color-background markdown for tier badges in dataframes.

    Streamlit's built-in palette is limited; the mapping picks the nearest
    semantic equivalent (A=blue ≈ cobalt primary, B=orange ≈ warm gold,
    C/D=gray). Not pixel-perfect to the design tokens, but consistent.
    """
    if not tier:
        return "—"
    return _TIER_BADGE.get(tier.upper(), "—")


def status_pill(state: str) -> str:
    """Streamlit color-background markdown for delivery-state badges."""
    return _STATUS_BADGE.get(state, "—")


def status_badge(ok: bool) -> str:
    return ":green-background[**OK**]" if ok else ":red-background[**ERR**]"


def pill(text: str, color: str = "blue") -> str:
    return f":{color}-background[{text}]"


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
