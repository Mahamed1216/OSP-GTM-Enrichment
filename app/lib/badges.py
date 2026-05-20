"""Markdown badge helpers — tier (A=green, B=amber, C=gray) and generic pills."""
from __future__ import annotations

_TIER_COLORS: dict[str, str] = {
    "A": "green",
    "B": "orange",
    "C": "gray",
}


def tier_badge(tier: str | None) -> str:
    if not tier:
        return ":gray-background[**—**]"
    color = _TIER_COLORS.get(tier.upper(), "blue")
    return f":{color}-background[**{tier.upper()}**]"


def status_badge(ok: bool) -> str:
    return ":green-background[**OK**]" if ok else ":red-background[**ERR**]"


def pill(text: str, color: str = "blue") -> str:
    return f":{color}-background[{text}]"


def tier_pill_html(tier: str | None) -> str:
    """HTML tier pill for use inside st.markdown(unsafe_allow_html=True).
    For dataframe cells use `tier_badge()` (cells don't render HTML)."""
    t = (tier or "").upper().strip()
    css_class = f"tier-{t.lower()}" if t else "tier-none"
    label = t if t else "—"
    return f'<span class="tier-pill {css_class}">{label}</span>'
