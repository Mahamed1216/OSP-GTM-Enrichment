"""Global CSS injection for the SDR enablement UI.

Call `inject_styles()` once at the top of every page (Streamlit renders
each page as its own script run; CSS injected in `main.py` does not
auto-propagate to page renders).

Design system: see app/PHASE8C_PLAN.md.
"""
from __future__ import annotations

import streamlit as st


_CSS = """
<style>
/* ============================================================ */
/* 1. Fonts                                                     */
/* ============================================================ */
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT,WONK@9..144,100..900,30..100,0..1&family=Geist:wght@100..900&family=JetBrains+Mono:wght@100..800&display=swap');

/* ============================================================ */
/* 2. Design tokens                                             */
/* ============================================================ */
:root {
  --bg: #FAF8F4;
  --bg-subtle: #F2EFE9;
  --sidebar-bg: #F4F1EB;
  --fg: #0F0E0C;
  --muted: #6B6660;
  --hairline: rgba(15, 14, 12, 0.10);
  --hairline-strong: rgba(15, 14, 12, 0.18);
  --accent: #1F3DE5;
  --accent-hover: #1632C2;
  --accent-tint: rgba(31, 61, 229, 0.10);
  --neutral-chip: #E8E5DE;
  --font-display: 'Fraunces', Georgia, 'Times New Roman', serif;
  --font-body: 'Geist', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace;
}

/* ============================================================ */
/* 3. Base canvas                                               */
/* ============================================================ */
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background: var(--bg) !important;
  color: var(--fg);
  font-family: var(--font-body);
  font-size: 15px;
  line-height: 1.6;
  font-feature-settings: "ss01", "cv11";
}

[data-testid="stHeader"] {
  background: transparent !important;
  border-bottom: 1px solid var(--hairline);
}

/* ============================================================ */
/* 4. Typography                                                */
/* ============================================================ */
h1, .stMarkdown h1, [data-testid="stMarkdownContainer"] h1 {
  font-family: var(--font-display) !important;
  font-weight: 500 !important;
  font-size: 44px !important;
  line-height: 1.1 !important;
  letter-spacing: -0.01em;
  font-variation-settings: 'opsz' 144, 'SOFT' 50, 'WONK' 1;
  color: var(--fg);
  margin: 0.4rem 0 1.2rem 0;
}

h2, .stMarkdown h2 {
  font-family: var(--font-body) !important;
  font-weight: 600 !important;
  font-size: 20px !important;
  line-height: 1.3 !important;
  letter-spacing: -0.005em;
  color: var(--fg);
  margin: 2rem 0 0.8rem 0;
  padding-bottom: 0.6rem;
  border-bottom: 1px solid var(--hairline);
}

h3, .stMarkdown h3 {
  font-family: var(--font-body) !important;
  font-weight: 600 !important;
  font-size: 16px !important;
  color: var(--fg);
  margin: 1.4rem 0 0.6rem 0;
}

h4, .stMarkdown h4 {
  font-family: var(--font-body) !important;
  font-weight: 500 !important;
  font-size: 14px !important;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin: 1rem 0 0.4rem 0;
}

p, .stMarkdown p, .stMarkdown li {
  font-family: var(--font-body);
  color: var(--fg);
}

[data-testid="stCaptionContainer"], .stCaption,
small, .stMarkdown small {
  color: var(--muted) !important;
  font-size: 13px !important;
  line-height: 1.55 !important;
  font-family: var(--font-body);
}

code, pre, kbd, samp {
  font-family: var(--font-mono) !important;
  font-feature-settings: "tnum";
}

/* ============================================================ */
/* 5. Layout — breathing room                                   */
/* ============================================================ */
[data-testid="stMain"] .block-container {
  padding-top: 3rem !important;
  padding-bottom: 4rem !important;
  max-width: 1180px;
}

/* ============================================================ */
/* 6. Sidebar                                                   */
/* ============================================================ */
[data-testid="stSidebar"] {
  background: var(--sidebar-bg) !important;
  border-right: 1px solid var(--hairline);
}
[data-testid="stSidebar"] > div:first-child {
  padding-top: 1.5rem;
}

[data-testid="stSidebarNav"] {
  padding: 0.5rem 0;
}

[data-testid="stSidebarNavLink"],
[data-testid="stSidebarNav"] a {
  font-family: var(--font-body) !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  letter-spacing: 0.01em;
  color: var(--fg) !important;
  border-radius: 0 !important;
  padding: 0.55rem 1rem !important;
  border-left: 3px solid transparent;
  transition: background 120ms ease;
}

[data-testid="stSidebarNavLink"]:hover,
[data-testid="stSidebarNav"] a:hover {
  background: rgba(15, 14, 12, 0.04) !important;
}

[data-testid="stSidebarNavLink"][aria-current="page"],
[data-testid="stSidebarNav"] a[aria-current="page"] {
  border-left: 3px solid var(--accent);
  background: transparent !important;
  color: var(--accent) !important;
  font-weight: 600 !important;
}

/* Defensive: always show the sidebar-collapsed expand chevron.
   Without this the user has no way to reopen the sidebar after
   clicking X — must refresh the page. Covers all three Streamlit
   version variants for the collapsed-control testid. */
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"],
button[kind="header"] {
  display: flex !important;
  visibility: visible !important;
  opacity: 1 !important;
  z-index: 999999 !important;
  position: fixed !important;
  top: 0.5rem !important;
  left: 0.5rem !important;
}

/* ============================================================ */
/* 7. Buttons                                                   */
/* ============================================================ */
.stButton > button,
.stFormSubmitButton > button,
[data-testid="baseButton-primary"],
[data-testid="baseButton-secondary"],
[data-testid="baseButton-primaryFormSubmit"],
[data-testid="baseButton-secondaryFormSubmit"] {
  font-family: var(--font-body) !important;
  font-weight: 500 !important;
  font-size: 14px !important;
  letter-spacing: 0.005em;
  border-radius: 6px !important;
  padding: 0.55rem 1.15rem !important;
  box-shadow: none !important;
  transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
}

[data-testid="baseButton-primary"],
[data-testid="baseButton-primaryFormSubmit"] {
  background: var(--accent) !important;
  color: var(--bg) !important;
  border: 1px solid var(--accent) !important;
}
[data-testid="baseButton-primary"]:hover,
[data-testid="baseButton-primaryFormSubmit"]:hover {
  background: var(--accent-hover) !important;
  border-color: var(--accent-hover) !important;
  color: var(--bg) !important;
}

[data-testid="baseButton-secondary"],
[data-testid="baseButton-secondaryFormSubmit"] {
  background: var(--bg) !important;
  color: var(--accent) !important;
  border: 1px solid var(--accent) !important;
}
[data-testid="baseButton-secondary"]:hover,
[data-testid="baseButton-secondaryFormSubmit"]:hover {
  background: var(--accent-tint) !important;
  color: var(--accent) !important;
}

[data-testid="baseButton-primary"]:disabled,
[data-testid="baseButton-secondary"]:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* ============================================================ */
/* 8. Inputs                                                    */
/* ============================================================ */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stNumberInput"] input,
[data-testid="stDateInput"] input,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div,
[data-testid="stMultiSelect"] div[data-baseweb="select"] > div {
  background: var(--bg) !important;
  border: 1px solid var(--hairline-strong) !important;
  border-radius: 4px !important;
  font-family: var(--font-body) !important;
  color: var(--fg) !important;
  font-size: 14px !important;
  box-shadow: none !important;
}

[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus,
[data-testid="stNumberInput"] input:focus {
  outline: 2px solid var(--accent) !important;
  outline-offset: -2px !important;
  border-color: var(--accent) !important;
}

[data-testid="stFileUploader"] section {
  background: var(--bg) !important;
  border: 1px dashed var(--hairline-strong) !important;
  border-radius: 6px !important;
}
[data-testid="stFileUploader"] section:hover {
  border-color: var(--accent) !important;
  background: var(--accent-tint) !important;
}

label, [data-testid="stWidgetLabel"] {
  font-family: var(--font-body) !important;
  font-size: 13px !important;
  color: var(--muted) !important;
  font-weight: 500 !important;
  letter-spacing: 0.01em;
}

/* Toggle / checkbox accent */
[data-testid="stToggle"] label > div[role="checkbox"][aria-checked="true"],
[data-testid="stCheckbox"] label > span > div[aria-checked="true"] {
  background: var(--accent) !important;
  border-color: var(--accent) !important;
}

/* ============================================================ */
/* 9. st.metric (native — kept for a few non-headline spots)    */
/* ============================================================ */
[data-testid="stMetric"] {
  background: transparent !important;
  border: none !important;
  padding: 0.5rem 0 !important;
}
[data-testid="stMetricLabel"] {
  font-family: var(--font-body) !important;
  font-size: 11px !important;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted) !important;
  font-weight: 500 !important;
}
[data-testid="stMetricValue"] {
  font-family: var(--font-mono) !important;
  font-size: 30px !important;
  font-weight: 500 !important;
  color: var(--fg) !important;
  font-variant-numeric: tabular-nums;
}
[data-testid="stMetricDelta"] {
  font-family: var(--font-mono) !important;
  font-size: 12px !important;
  color: var(--muted) !important;
}

/* ============================================================ */
/* 10. st.dataframe                                             */
/* ============================================================ */
[data-testid="stDataFrame"] {
  border: 1px solid var(--hairline) !important;
  border-radius: 4px !important;
  overflow: hidden;
}
[data-testid="stDataFrame"] thead th {
  font-family: var(--font-body) !important;
  font-size: 11px !important;
  text-transform: uppercase !important;
  letter-spacing: 0.06em !important;
  color: var(--muted) !important;
  font-weight: 500 !important;
  background: var(--bg) !important;
  border-bottom: 1px solid var(--hairline-strong) !important;
}
[data-testid="stDataFrame"] tbody td {
  font-family: var(--font-body) !important;
  font-size: 13px !important;
  color: var(--fg) !important;
  border-bottom: 1px solid var(--hairline) !important;
}

/* ============================================================ */
/* 11. st.tabs                                                  */
/* ============================================================ */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
  border-bottom: 1px solid var(--hairline) !important;
  gap: 0 !important;
}
[data-testid="stTabs"] button[role="tab"] {
  font-family: var(--font-body) !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  color: var(--muted) !important;
  background: transparent !important;
  padding: 0.7rem 1.2rem !important;
  border-bottom: 2px solid transparent !important;
}
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
  color: var(--accent) !important;
  border-bottom: 2px solid var(--accent) !important;
  background: transparent !important;
}

/* ============================================================ */
/* 12. st.expander                                              */
/* ============================================================ */
[data-testid="stExpander"] {
  border: 1px solid var(--hairline) !important;
  border-radius: 4px !important;
  background: var(--bg) !important;
  box-shadow: none !important;
}
[data-testid="stExpander"] summary {
  font-family: var(--font-body) !important;
  font-size: 14px !important;
  font-weight: 500 !important;
  color: var(--fg) !important;
}
[data-testid="stExpander"] summary:hover {
  background: var(--accent-tint) !important;
}

/* ============================================================ */
/* 13. st.status                                                */
/* ============================================================ */
[data-testid="stStatusWidget"], [data-testid="stStatus"] {
  border: 1px solid var(--hairline) !important;
  border-radius: 4px !important;
  background: var(--bg) !important;
  font-family: var(--font-body) !important;
}

/* ============================================================ */
/* 14. Alerts — restyle from saturated to hairline + tint       */
/* ============================================================ */
[data-testid="stAlert"] {
  border-radius: 4px !important;
  border: 1px solid var(--hairline-strong) !important;
  background: var(--bg) !important;
  font-family: var(--font-body) !important;
  color: var(--fg) !important;
  padding: 0.85rem 1rem !important;
  box-shadow: none !important;
}
[data-testid="stAlert"][data-baseweb="notification"] [data-testid="stMarkdownContainer"] {
  color: var(--fg) !important;
}
/* success */
div[data-testid="stAlert"]:has(svg[data-testid="stIconSuccess"]) {
  border-left: 3px solid var(--accent) !important;
  background: var(--accent-tint) !important;
}
/* info */
div[data-testid="stAlert"]:has(svg[data-testid="stIconInfo"]) {
  border-left: 3px solid var(--muted) !important;
}
/* warning */
div[data-testid="stAlert"]:has(svg[data-testid="stIconWarning"]) {
  border-left: 3px solid #C8861F !important;
  background: rgba(200, 134, 31, 0.06) !important;
}
/* error */
div[data-testid="stAlert"]:has(svg[data-testid="stIconError"]) {
  border-left: 3px solid #8B2138 !important;
  background: rgba(139, 33, 56, 0.06) !important;
}

/* ============================================================ */
/* 15. Divider                                                  */
/* ============================================================ */
[data-testid="stDivider"], hr, [data-testid="stMarkdownContainer"] hr {
  border: none !important;
  border-top: 1px solid var(--hairline) !important;
  margin: 2rem 0 !important;
}

/* ============================================================ */
/* 16. Markdown background-pills (Streamlit :color-background)  */
/* ============================================================ */
/* Streamlit renders :color-background[**X**] as a span with an
   rgba background-color. We strip the default and apply the unified
   tier-pill aesthetic, picking variants from the rendered color. */
[data-testid="stMarkdownContainer"] span[style*="background-color"] {
  font-family: var(--font-mono) !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  letter-spacing: 0.04em !important;
  padding: 2px 9px !important;
  border-radius: 4px !important;
  line-height: 18px !important;
  display: inline-block;
}

/* Green background → Tier A (cobalt fill) */
[data-testid="stMarkdownContainer"] span[style*="rgba(33, 195, 84"],
[data-testid="stMarkdownContainer"] span[style*="rgb(33, 195, 84"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(212, 233, 217)"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgba(33,195,84"] {
  background: var(--accent) !important;
  color: var(--bg) !important;
}

/* Orange background → Tier B (accent-tint with cobalt text) */
[data-testid="stMarkdownContainer"] span[style*="rgba(255, 137, 0"],
[data-testid="stMarkdownContainer"] span[style*="rgb(255, 137, 0"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(255, 226, 188)"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgba(255,137,0"] {
  background: var(--accent-tint) !important;
  color: var(--accent) !important;
}

/* Gray background → Tier C (neutral chip) */
[data-testid="stMarkdownContainer"] span[style*="rgba(155, 158, 161"],
[data-testid="stMarkdownContainer"] span[style*="rgb(155, 158, 161"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(225, 227, 229)"] {
  background: var(--neutral-chip) !important;
  color: var(--fg) !important;
}

/* Red background → error chip */
[data-testid="stMarkdownContainer"] span[style*="rgba(255, 43, 43"],
[data-testid="stMarkdownContainer"] span[style*="rgb(255, 43, 43"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(255, 219, 219)"] {
  background: rgba(139, 33, 56, 0.10) !important;
  color: #8B2138 !important;
}

/* ============================================================ */
/* 17. Custom components                                        */
/* ============================================================ */
.kpi-card {
  background: var(--bg);
  border: 1px solid var(--hairline);
  border-radius: 4px;
  padding: 18px 20px;
  min-height: 124px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
}
.kpi-card::before {
  content: "";
  position: absolute;
  left: 0;
  top: 18px;
  bottom: 18px;
  width: 2px;
  background: var(--accent);
  opacity: 0;
  transition: opacity 200ms ease;
}
.kpi-card:hover::before { opacity: 1; }
.kpi-label {
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
}
.kpi-value-mono {
  font-family: var(--font-mono);
  font-size: 32px;
  font-weight: 500;
  color: var(--fg);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
  margin-top: 4px;
}
.kpi-value-serif {
  font-family: var(--font-display);
  font-weight: 500;
  font-size: 40px;
  font-variation-settings: 'opsz' 144, 'SOFT' 50, 'WONK' 1;
  color: var(--fg);
  line-height: 1.05;
  margin-top: 4px;
}
.kpi-sublabel {
  font-family: var(--font-body);
  font-size: 12px;
  color: var(--muted);
  margin-top: 4px;
}

.tier-pill {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  padding: 2px 9px;
  border-radius: 4px;
  line-height: 18px;
  display: inline-block;
}
.tier-pill.tier-a { background: var(--accent); color: var(--bg); }
.tier-pill.tier-b { background: var(--accent-tint); color: var(--accent); }
.tier-pill.tier-c { background: var(--neutral-chip); color: var(--fg); }
.tier-pill.tier-d { background: transparent; border: 1px solid var(--hairline-strong); color: var(--muted); padding: 1px 8px; }
.tier-pill.tier-none { background: transparent; color: var(--muted); }

/* ============================================================ */
/* 18. Misc tweaks                                              */
/* ============================================================ */
/* (Previously hid stToolbar wholesale; that nuked the collapsed-sidebar
   expand chevron in some Streamlit versions. Leaving the toolbar visible
   is the safer default — its content is minimal in headless mode.) */

.stCodeBlock, [data-testid="stCodeBlock"] pre {
  background: var(--bg-subtle) !important;
  border: 1px solid var(--hairline) !important;
  border-radius: 4px !important;
  font-family: var(--font-mono) !important;
  font-size: 13px !important;
}

[data-testid="stJson"] {
  background: var(--bg-subtle) !important;
  border: 1px solid var(--hairline) !important;
  border-radius: 4px !important;
  font-family: var(--font-mono) !important;
}

@media (max-width: 900px) {
  [data-testid="stMain"] .block-container {
    padding-top: 1.5rem !important;
    padding-bottom: 2rem !important;
  }
  h1, .stMarkdown h1 { font-size: 32px !important; }
}
</style>
"""


def inject_styles() -> None:
    """Inject the global stylesheet. Call at the top of every page."""
    st.markdown(_CSS, unsafe_allow_html=True)
