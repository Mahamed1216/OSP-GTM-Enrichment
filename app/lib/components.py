"""Custom HTML components rendered via st.markdown(unsafe_allow_html=True).

CSS for these classes lives in app/styles.py.
"""
from __future__ import annotations

from html import escape

import streamlit as st


def kpi_card(
    label: str,
    value: str | int | float,
    sublabel: str | None = None,
    numeric_font: str = "mono",
) -> None:
    """Render a styled KPI card.

    numeric_font: 'mono' for data numbers (e.g. 1,243), 'serif' for
    story numbers (e.g. a headline reply rate where the type itself
    is the point).
    """
    font_class = "kpi-value-serif" if numeric_font == "serif" else "kpi-value-mono"
    sub_html = (
        f'<div class="kpi-sublabel">{escape(str(sublabel))}</div>' if sublabel else ""
    )
    st.markdown(
        f'<div class="kpi-card">'
        f'<div class="kpi-label">{escape(str(label))}</div>'
        f'<div class="{font_class}">{escape(str(value))}</div>'
        f'{sub_html}'
        f'</div>',
        unsafe_allow_html=True,
    )
