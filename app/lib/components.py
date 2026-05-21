"""Custom HTML components rendered via st.markdown(unsafe_allow_html=True).

CSS for these classes lives in app/styles.py.
"""
from __future__ import annotations

from html import escape
from typing import Iterable

import streamlit as st


# Editorial-clean monochrome glyphs used in lieu of an icon font. Spec
# named Tabler outline icons; we render unicode equivalents so the UI has
# no extra network dependency on Google Fonts beyond the typefaces.
_ICON_GLYPHS: dict[str, str] = {
    "ti-target": "◎",
    "ti-mail-check": "✓",
    "ti-send": "↗",
    "ti-message-2": "↩",
    "ti-flame": "✦",
    "ti-bolt": "⚡",
    "ti-eye": "◉",
}


def _icon_glyph(name: str) -> str:
    if not name:
        return ""
    return _ICON_GLYPHS.get(name, name)


def kpi_card(
    label: str,
    value: str | int | float,
    sublabel: str | None = None,
    numeric_font: str = "mono",
    *,
    meta: str = "",
    icon: str = "",
    state: str = "neutral",
) -> None:
    """Render a styled KPI card.

    state — colour-encoded top-rule: 'primary' (cobalt), 'success', 'warning',
    'danger', or 'neutral'. icon — a key from _ICON_GLYPHS (e.g. 'ti-target')
    or any short glyph; rendered inline left of the label. `sublabel` and
    `meta` are aliases (meta wins if both passed) so 8e call-sites keep
    working unchanged.
    """
    font_class = "kpi-value-serif" if numeric_font == "serif" else "kpi-value-mono"
    sub_text = meta or sublabel or ""
    sub_html = (
        f'<div class="kpi-sublabel">{escape(str(sub_text))}</div>' if sub_text else ""
    )
    glyph = _icon_glyph(icon)
    icon_html = (
        f'<span class="kpi-icon kpi-icon-{state}">{escape(glyph)}</span>' if glyph else ""
    )
    st.markdown(
        f'<div class="kpi-card kpi-state-{state}">'
        f'<div class="kpi-label">{icon_html}<span>{escape(str(label))}</span></div>'
        f'<div class="{font_class}">{escape(str(value))}</div>'
        f'{sub_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def fit_score_viz(score: int | float | None, tier: str | None) -> str:
    """HTML for the Lead Detail → Scoring fit-score visualization.

    Renders: tier pill + big serif score + tiered horizontal track with a
    cobalt fill and marker at the score's position, then a 0/40/60/80/100
    scale beneath. Returns a string; caller passes through st.markdown with
    unsafe_allow_html=True.
    """
    tier_letter = (tier or "").upper() or "—"
    tier_class = tier_letter.lower() if tier_letter in {"A", "B", "C", "D"} else ""
    try:
        score_int = int(round(float(score))) if score is not None else 0
    except (TypeError, ValueError):
        score_int = 0
    score_int = max(0, min(100, score_int))
    return (
        '<div class="fit-viz">'
        '<div class="fit-viz-header">'
        f'<span class="fit-viz-tier tier-{tier_class}">{escape(tier_letter)}</span>'
        f'<span class="fit-viz-score">{score_int}</span>'
        '<span class="fit-viz-suffix">/ 100</span>'
        '</div>'
        '<div class="fit-viz-track">'
        '<span class="fit-viz-band band-d"></span>'
        '<span class="fit-viz-band band-c"></span>'
        '<span class="fit-viz-band band-b"></span>'
        '<span class="fit-viz-band band-a"></span>'
        f'<span class="fit-viz-fill" style="width: {score_int}%;"></span>'
        f'<span class="fit-viz-marker" style="left: {score_int}%;"></span>'
        '</div>'
        '<div class="fit-viz-scale">'
        '<span>0</span><span>40</span><span>60</span><span>80</span><span>100</span>'
        '</div>'
        '</div>'
    )


def leads_by_tier_chart(counts: dict[str, int]) -> str:
    """Horizontal bar chart for tier distribution.

    `counts` is keyed by tier letter (A/B/C/D) plus "—" for unscored.
    Bars width-encode proportion of the largest bucket; tier colour follows
    the tier palette (unscored uses a neutral hairline-strong fill).
    """
    max_count = max(counts.values()) if counts else 0
    if not max_count:
        max_count = 1
    bars: list[str] = []
    for tier, count in counts.items():
        pct = int(round((count / max_count) * 100))
        if tier in {"A", "B", "C", "D"}:
            tier_class = f"tier-{tier.lower()}"
            label = f"Tier {tier}"
        else:
            tier_class = "tier-none"
            label = "Unscored"
        bars.append(
            f'<div class="ltc-row">'
            f'<span class="ltc-label">{escape(label)}</span>'
            f'<div class="ltc-track">'
            f'<div class="ltc-bar {tier_class}" style="width: {pct}%;"></div>'
            f'</div>'
            f'<span class="ltc-count">{int(count)}</span>'
            f'</div>'
        )
    return (
        '<div class="leads-by-tier">'
        '<div class="ltc-title">Leads by tier</div>'
        + "".join(bars)
        + '</div>'
    )


def promo_block(
    headline: str,
    body: str,
    cta_label: str,
    cta_target_page: str,
    *,
    key_suffix: str = "",
) -> None:
    """Cobalt-fill CTA. Renders the headline/body, then a Streamlit button.

    Clicking the button calls st.switch_page(cta_target_page). The button
    sits in a separate Streamlit element so its rendering is real Streamlit
    interaction (not HTML). CSS in app/styles.py styles the adjacent
    `.promo-block + ...` element so the button picks up the white-on-cobalt
    treatment that matches the block above it.
    """
    st.markdown(
        f'<div class="promo-block">'
        f'<div class="promo-headline">{escape(headline)}</div>'
        f'<div class="promo-body">{escape(body)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    btn_key = f"promo_{cta_target_page}_{key_suffix}".rstrip("_")
    if st.button(cta_label, key=btn_key, type="primary"):
        st.switch_page(cta_target_page)


def activity_row(kind: str, text: str, when: str) -> str:
    """Single row for the recent-activity feed; returns HTML."""
    dot_class = {
        "enrich": "dot-primary",
        "gen": "dot-primary",
        "send": "dot-warning",
        "reply": "dot-success",
        "rate": "dot-neutral",
    }.get(kind, "dot-neutral")
    return (
        '<div class="activity-row">'
        f'<span class="activity-dot {dot_class}"></span>'
        f'<span class="activity-text">{escape(text)}</span>'
        f'<span class="activity-when">{escape(when)}</span>'
        '</div>'
    )


_ACTIVITY_EMPTY_HTML = '<div class="activity-empty">No recent activity.</div>'


def activity_feed(rows: Iterable[str], *, title: str = "Recent activity") -> str:
    """Wrap a sequence of activity_row() outputs into a feed card."""
    rows_html = "".join(rows) or _ACTIVITY_EMPTY_HTML
    return (
        '<div class="activity-feed">'
        f'<div class="activity-title">{escape(title)}</div>'
        f'{rows_html}'
        '</div>'
    )
