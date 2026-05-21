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
    "A": "🟦 A",
    "B": "🟧 B",
    "C": "⬜ C",
    "D": "⬛ D",
}

_STATUS_BADGE: dict[str, str] = {
    "sent":    "🟦 Sent",
    "replied": "🟩 Replied",
    "opened":  "🟧 Opened",
    "bounced": "🟥 Bounced",
    "pending": "⬜ Pending",
}


def tier_badge(tier: str | None) -> str:
    """Tier badge for st.dataframe cells. Colored unicode square + letter.

    st.dataframe cells render plain text — they don't parse Streamlit's
    `:color-background[]` markdown shorthand and they don't render HTML.
    Coloured unicode squares are the only reliable way to get visible
    colour into a dataframe cell.
    """
    if not tier:
        return "—"
    return _TIER_BADGE.get(tier.upper(), "—")


def status_pill(state: str) -> str:
    """Delivery-state badge for st.dataframe cells. Colored unicode square + label."""
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
