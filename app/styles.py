"""Global CSS injection for the SDR enablement UI.

Call `inject_styles()` once at the top of every page (Streamlit renders
each page as its own script run; CSS injected in `main.py` does not
auto-propagate to page renders).

Phase 8d editorial polish: Stripe Press / Linear aesthetic. No shadows.
Hairline borders, generous spacing, Fraunces display + Geist body +
JetBrains Mono numerics.
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
  /* Core palette */
  --paper: #FAF8F4;
  --paper-subtle: #F2EFE9;
  --surface: #F4F1EB;
  --ink: #0F0E0C;
  --ink-muted: #6E6A63;
  --hairline: rgba(15, 14, 12, 0.08);
  --hairline-strong: rgba(15, 14, 12, 0.16);
  --cobalt: #1F3DE5;
  --cobalt-hover: #1730BD;
  --cobalt-tint: rgba(31, 61, 229, 0.06);

  /* Tier palette */
  --tier-a: #1F3DE5;
  --tier-b: #A8762E;
  --tier-c: #6E6A63;
  --tier-d: #8E8A83;

  /* Semantic */
  --color-success: #1F7A4D;
  --color-warning: #A8762E;
  --color-error:   #A82E2E;

  /* Spacing scale */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-6: 24px;
  --space-8: 32px;
  --space-12: 48px;
  --space-16: 64px;
  --space-24: 96px;

  /* Type */
  --font-display: 'Fraunces', Georgia, 'Times New Roman', serif;
  --font-body: 'Geist', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace;

  /* Legacy aliases (retained so any existing var refs still resolve) */
  --bg: var(--paper);
  --bg-subtle: var(--paper-subtle);
  --sidebar-bg: var(--surface);
  --fg: var(--ink);
  --muted: var(--ink-muted);
  --accent: var(--cobalt);
  --accent-hover: var(--cobalt-hover);
  --accent-tint: var(--cobalt-tint);
  --neutral-chip: #E8E5DE;
}

/* ============================================================ */
/* 3. Base canvas — paper grain via fixed SVG noise              */
/* ============================================================ */
html {
  background-color: var(--paper) !important;
  color: var(--ink);
}
body {
  background-color: var(--paper);
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.05 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
  background-attachment: fixed;
  color: var(--ink);
}
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  font-family: var(--font-body);
  font-size: 15.5px;
  line-height: 1.65;
  font-feature-settings: "ss01", "cv11";
  -webkit-font-smoothing: antialiased;
}
[data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background: transparent !important;
}

[data-testid="stHeader"] {
  background: transparent !important;
  border-bottom: 1px solid var(--hairline);
  box-shadow: none !important;
}

/* Global focus ring */
*:focus-visible {
  outline: 2px solid var(--cobalt);
  outline-offset: 2px;
  border-radius: 2px;
}

/* ============================================================ */
/* 4. Typography                                                */
/* ============================================================ */
h1, .stMarkdown h1, [data-testid="stMarkdownContainer"] h1 {
  font-family: var(--font-display) !important;
  font-weight: 400 !important;
  font-size: 56px !important;
  line-height: 1.05 !important;
  letter-spacing: -0.02em !important;
  font-variation-settings: 'opsz' 144, 'SOFT' 50, 'WONK' 1;
  color: var(--ink);
  margin: 0 0 var(--space-3) 0;
}

h2, .stMarkdown h2, [data-testid="stMarkdownContainer"] h2 {
  font-family: var(--font-display) !important;
  font-weight: 400 !important;
  font-size: 32px !important;
  line-height: 1.15 !important;
  letter-spacing: -0.015em !important;
  font-variation-settings: 'opsz' 96, 'SOFT' 50, 'WONK' 0;
  color: var(--ink);
  margin: var(--space-12) 0 var(--space-4) 0;
  padding-bottom: 0;
  border-bottom: none;
}

h3, .stMarkdown h3, [data-testid="stMarkdownContainer"] h3 {
  font-family: var(--font-display) !important;
  font-weight: 500 !important;
  font-size: 22px !important;
  line-height: 1.25 !important;
  letter-spacing: -0.01em !important;
  color: var(--ink);
  margin: var(--space-8) 0 var(--space-3) 0;
}

h4, .stMarkdown h4 {
  font-family: var(--font-body) !important;
  font-weight: 500 !important;
  font-size: 11px !important;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--ink-muted);
  margin: var(--space-6) 0 var(--space-2) 0;
}

p, .stMarkdown p, .stMarkdown li {
  font-family: var(--font-body);
  font-size: 15.5px;
  line-height: 1.65;
  color: var(--ink);
}

[data-testid="stCaptionContainer"], .stCaption,
small, .stMarkdown small {
  color: var(--ink-muted) !important;
  font-size: 13px !important;
  line-height: 1.5 !important;
  font-family: var(--font-body);
}

code, pre, kbd, samp {
  font-family: var(--font-mono) !important;
  font-feature-settings: "tnum";
}

/* Reusable utility classes */
.caps-label {
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--ink-muted);
}
.meta {
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--ink-muted);
  line-height: 1.5;
}
.numeric {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}

/* Page-header pattern */
.page-header {
  margin: 0 0 var(--space-12) 0;
}
.page-header h1 { margin: 0; }
.page-header .page-subtitle {
  margin-top: var(--space-2);
  font-family: var(--font-body);
  font-size: 14px;
  color: var(--ink-muted);
  line-height: 1.55;
}

/* ============================================================ */
/* 5. Layout — breathing room                                   */
/* ============================================================ */
[data-testid="stMain"] .block-container {
  padding-top: var(--space-12) !important;
  padding-bottom: var(--space-16) !important;
  max-width: 1180px;
}

/* ============================================================ */
/* 6. Sidebar                                                   */
/* ============================================================ */
[data-testid="stSidebar"] {
  background: var(--surface) !important;
  border-right: 1px solid var(--hairline);
  box-shadow: none !important;
}
[data-testid="stSidebar"] > div:first-child {
  padding-top: var(--space-6);
}

[data-testid="stSidebarNav"] {
  padding: var(--space-2) 0;
}

[data-testid="stSidebarNavLink"],
[data-testid="stSidebarNav"] a {
  font-family: var(--font-body) !important;
  font-size: 14px !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
  color: var(--ink-muted) !important;
  border-radius: 0 !important;
  padding: 10px 16px !important;
  border-left: 2px solid transparent;
  transition: color 120ms ease, background 120ms ease;
}

[data-testid="stSidebarNavLink"]:hover,
[data-testid="stSidebarNav"] a:hover {
  color: var(--ink) !important;
  background: transparent !important;
}

[data-testid="stSidebarNavLink"][aria-current="page"],
[data-testid="stSidebarNav"] a[aria-current="page"] {
  border-left: 2px solid var(--cobalt);
  padding-left: 14px !important;
  background: transparent !important;
  color: var(--cobalt) !important;
  font-weight: 500 !important;
}

/* Defensive: always show the sidebar-collapsed expand chevron. */
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
  letter-spacing: 0 !important;
  text-transform: none !important;
  border-radius: 8px !important;
  padding: 10px 20px !important;
  box-shadow: none !important;
  transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
}

[data-testid="baseButton-primary"],
[data-testid="baseButton-primaryFormSubmit"] {
  background: var(--cobalt) !important;
  color: #FFFFFF !important;
  border: 1px solid var(--cobalt) !important;
}
[data-testid="baseButton-primary"]:hover,
[data-testid="baseButton-primaryFormSubmit"]:hover {
  background: var(--cobalt-hover) !important;
  border-color: var(--cobalt-hover) !important;
  color: #FFFFFF !important;
}

[data-testid="baseButton-secondary"],
[data-testid="baseButton-secondaryFormSubmit"] {
  background: transparent !important;
  color: var(--ink) !important;
  border: 1px solid var(--ink) !important;
}
[data-testid="baseButton-secondary"]:hover,
[data-testid="baseButton-secondaryFormSubmit"]:hover {
  background: var(--cobalt-tint) !important;
  border-color: var(--ink) !important;
  color: var(--ink) !important;
}

[data-testid="baseButton-primary"]:disabled,
[data-testid="baseButton-secondary"]:disabled,
.stButton > button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  box-shadow: none !important;
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
  background: var(--paper) !important;
  border: 1px solid var(--hairline-strong) !important;
  border-radius: 8px !important;
  font-family: var(--font-body) !important;
  color: var(--ink) !important;
  font-size: 14px !important;
  box-shadow: none !important;
}

[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus,
[data-testid="stNumberInput"] input:focus {
  outline: 2px solid var(--cobalt) !important;
  outline-offset: -2px !important;
  border-color: var(--cobalt) !important;
}

[data-testid="stFileUploader"] section {
  background: var(--paper) !important;
  border: 1px dashed var(--hairline-strong) !important;
  border-radius: 8px !important;
  box-shadow: none !important;
}
[data-testid="stFileUploader"] section:hover {
  border-color: var(--cobalt) !important;
  background: var(--cobalt-tint) !important;
}

label, [data-testid="stWidgetLabel"] {
  font-family: var(--font-body) !important;
  font-size: 13px !important;
  color: var(--ink-muted) !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
}

/* Toggle / checkbox accent */
[data-testid="stToggle"] label > div[role="checkbox"][aria-checked="true"],
[data-testid="stCheckbox"] label > span > div[aria-checked="true"] {
  background: var(--cobalt) !important;
  border-color: var(--cobalt) !important;
}

/* ============================================================ */
/* 9. st.metric (native — kept for a few non-headline spots)    */
/* ============================================================ */
[data-testid="stMetric"] {
  background: transparent !important;
  border: none !important;
  padding: var(--space-2) 0 !important;
  box-shadow: none !important;
}
[data-testid="stMetricLabel"] {
  font-family: var(--font-body) !important;
  font-size: 11px !important;
  text-transform: uppercase !important;
  letter-spacing: 0.12em !important;
  color: var(--ink-muted) !important;
  font-weight: 500 !important;
}
[data-testid="stMetricValue"] {
  font-family: var(--font-mono) !important;
  font-size: 32px !important;
  font-weight: 500 !important;
  color: var(--ink) !important;
  font-variant-numeric: tabular-nums;
}
[data-testid="stMetricDelta"] {
  font-family: var(--font-mono) !important;
  font-size: 12px !important;
  color: var(--ink-muted) !important;
}

/* ============================================================ */
/* 10. st.dataframe / tables                                    */
/* ============================================================ */
[data-testid="stDataFrame"], [data-testid="stTable"] {
  border: 1px solid var(--hairline) !important;
  border-radius: 0 !important;
  overflow: hidden;
  box-shadow: none !important;
}
[data-testid="stDataFrame"] thead th,
[data-testid="stTable"] thead th {
  font-family: var(--font-body) !important;
  font-size: 13px !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
  color: var(--ink) !important;
  font-weight: 500 !important;
  background: var(--paper) !important;
  border-bottom: 1px solid var(--ink) !important;
  border-right: none !important;
  border-left: none !important;
}
[data-testid="stDataFrame"] tbody td,
[data-testid="stTable"] tbody td {
  font-family: var(--font-body) !important;
  font-size: 14px !important;
  color: var(--ink) !important;
  border-bottom: 1px solid var(--hairline) !important;
  border-right: none !important;
  border-left: none !important;
}
[data-testid="stDataFrame"] tbody tr:hover td,
[data-testid="stTable"] tbody tr:hover td {
  background: rgba(15, 14, 12, 0.02) !important;
}
/* Tabular-nums on numeric-aligned cells (Streamlit applies text-align:right for numbers) */
[data-testid="stDataFrame"] tbody td[style*="text-align: right"],
[data-testid="stTable"] tbody td[style*="text-align: right"] {
  font-family: var(--font-mono) !important;
  font-variant-numeric: tabular-nums !important;
  font-weight: 500 !important;
}

/* ============================================================ */
/* 11. st.tabs — underline style                                */
/* ============================================================ */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
  border-bottom: 1px solid var(--hairline) !important;
  gap: 0 !important;
  background: transparent !important;
}
[data-testid="stTabs"] button[role="tab"] {
  font-family: var(--font-body) !important;
  font-size: 14px !important;
  font-weight: 500 !important;
  color: var(--ink-muted) !important;
  background: transparent !important;
  border: none !important;
  padding: 12px 0 !important;
  margin-right: var(--space-6) !important;
  margin-bottom: -1px !important;
  border-bottom: 2px solid transparent !important;
  border-radius: 0 !important;
  box-shadow: none !important;
  transition: color 120ms ease, border-color 120ms ease;
}
[data-testid="stTabs"] button[role="tab"]:hover {
  color: var(--ink) !important;
  background: transparent !important;
}
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
  color: var(--ink) !important;
  border-bottom: 2px solid var(--cobalt) !important;
  background: transparent !important;
}

/* ============================================================ */
/* 12. st.expander                                              */
/* ============================================================ */
[data-testid="stExpander"] {
  border: 1px solid var(--hairline) !important;
  border-radius: 8px !important;
  background: var(--paper) !important;
  box-shadow: none !important;
}
[data-testid="stExpander"] summary {
  font-family: var(--font-body) !important;
  font-size: 14px !important;
  font-weight: 500 !important;
  color: var(--ink) !important;
}
[data-testid="stExpander"] summary:hover {
  background: var(--cobalt-tint) !important;
}

/* ============================================================ */
/* 13. st.status                                                */
/* ============================================================ */
[data-testid="stStatusWidget"], [data-testid="stStatus"] {
  border: 1px solid var(--hairline) !important;
  border-radius: 8px !important;
  background: var(--paper) !important;
  font-family: var(--font-body) !important;
  box-shadow: none !important;
}

/* ============================================================ */
/* 14. Alerts — hairline + tint, no shadow                      */
/* ============================================================ */
[data-testid="stAlert"] {
  border-radius: 8px !important;
  border: 1px solid var(--hairline-strong) !important;
  background: var(--paper) !important;
  font-family: var(--font-body) !important;
  color: var(--ink) !important;
  padding: var(--space-3) var(--space-4) !important;
  box-shadow: none !important;
}
[data-testid="stAlert"][data-baseweb="notification"] [data-testid="stMarkdownContainer"] {
  color: var(--ink) !important;
}
div[data-testid="stAlert"]:has(svg[data-testid="stIconSuccess"]) {
  border-left: 3px solid var(--color-success) !important;
  background: rgba(31, 122, 77, 0.06) !important;
}
div[data-testid="stAlert"]:has(svg[data-testid="stIconInfo"]) {
  border-left: 3px solid var(--cobalt) !important;
  background: var(--cobalt-tint) !important;
}
div[data-testid="stAlert"]:has(svg[data-testid="stIconWarning"]) {
  border-left: 3px solid var(--color-warning) !important;
  background: rgba(168, 118, 46, 0.06) !important;
}
div[data-testid="stAlert"]:has(svg[data-testid="stIconError"]) {
  border-left: 3px solid var(--color-error) !important;
  background: rgba(168, 46, 46, 0.06) !important;
}

/* ============================================================ */
/* 15. Divider                                                  */
/* ============================================================ */
[data-testid="stDivider"], hr, [data-testid="stMarkdownContainer"] hr {
  border: none !important;
  border-top: 1px solid var(--hairline) !important;
  margin: var(--space-8) 0 !important;
}

/* ============================================================ */
/* 16. Markdown background-pills (Streamlit :color-background)  */
/* Map Streamlit's :green/:orange/:gray/:red-background into the */
/* tier-pill aesthetic so dataframe-injected badges look unified */
/* ============================================================ */
[data-testid="stMarkdownContainer"] span[style*="background-color"] {
  font-family: var(--font-body) !important;
  font-size: 11px !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
  padding: 2px 8px !important;
  border-radius: 2px !important;
  line-height: 18px !important;
  display: inline-block;
  text-transform: none !important;
}

/* Green → Tier A (cobalt fill) */
[data-testid="stMarkdownContainer"] span[style*="rgba(33, 195, 84"],
[data-testid="stMarkdownContainer"] span[style*="rgb(33, 195, 84"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(212, 233, 217)"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgba(33,195,84"] {
  background: var(--tier-a) !important;
  color: #FFFFFF !important;
}

/* Orange → Tier B */
[data-testid="stMarkdownContainer"] span[style*="rgba(255, 137, 0"],
[data-testid="stMarkdownContainer"] span[style*="rgb(255, 137, 0"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(255, 226, 188)"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgba(255,137,0"] {
  background: var(--tier-b) !important;
  color: #FFFFFF !important;
}

/* Gray → Tier C */
[data-testid="stMarkdownContainer"] span[style*="rgba(155, 158, 161"],
[data-testid="stMarkdownContainer"] span[style*="rgb(155, 158, 161"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(225, 227, 229)"] {
  background: var(--tier-c) !important;
  color: #FFFFFF !important;
}

/* Red → error chip */
[data-testid="stMarkdownContainer"] span[style*="rgba(255, 43, 43"],
[data-testid="stMarkdownContainer"] span[style*="rgb(255, 43, 43"],
[data-testid="stMarkdownContainer"] span[style*="background-color: rgb(255, 219, 219)"] {
  background: var(--color-error) !important;
  color: #FFFFFF !important;
}

/* ============================================================ */
/* 17. Custom components                                        */
/* ============================================================ */

/* Hero block (dashboard top) */
.hero-block {
  margin: 0 0 var(--space-16) 0;
}
.hero-headline {
  font-family: var(--font-display);
  font-weight: 400;
  font-size: 96px;
  line-height: 1.0;
  letter-spacing: -0.025em;
  font-variation-settings: 'opsz' 144, 'SOFT' 50, 'WONK' 1;
  color: var(--ink);
  margin: 0;
}
.hero-sublabel {
  font-family: var(--font-body);
  font-size: 18px;
  font-weight: 400;
  line-height: 1.45;
  color: var(--ink-muted);
  max-width: 540px;
  margin: var(--space-4) 0 0 0;
}

/* Generic card */
.card {
  background: var(--paper);
  border: 1px solid var(--hairline);
  border-radius: 8px;
  padding: var(--space-8);
  box-shadow: inset 0 0 0 0.5px rgba(15, 14, 12, 0.04);
  transition:
    border-color 120ms cubic-bezier(0.22, 1, 0.36, 1),
    transform 120ms cubic-bezier(0.22, 1, 0.36, 1);
}
.card:hover {
  border-color: var(--hairline-strong);
  transform: translateY(-1px);
}

/* KPI card — caps label → 8px → big numeric → 4px → meta */
.kpi-card {
  background: var(--paper);
  border: 1px solid var(--hairline);
  border-radius: 8px;
  padding: var(--space-8);
  min-height: 148px;
  display: flex;
  flex-direction: column;
  box-shadow: inset 0 0 0 0.5px rgba(15, 14, 12, 0.04);
  transition:
    border-color 120ms cubic-bezier(0.22, 1, 0.36, 1),
    transform 120ms cubic-bezier(0.22, 1, 0.36, 1);
}
.kpi-card:hover {
  border-color: var(--hairline-strong);
  transform: translateY(-1px);
}
.kpi-card .kpi-label {
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--ink-muted);
  margin: 0 0 var(--space-2) 0;
}
.kpi-card .kpi-value-mono,
.kpi-card .kpi-value-serif {
  font-family: var(--font-display);
  font-weight: 400;
  font-size: 72px;
  font-variation-settings: 'opsz' 144, 'SOFT' 50, 'WONK' 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  color: var(--ink);
  line-height: 1.0;
  margin: 0;
}
.kpi-card .kpi-sublabel {
  font-family: var(--font-body);
  font-size: 13px;
  line-height: 1.5;
  color: var(--ink-muted);
  margin: var(--space-1) 0 0 0;
}

/* Fit-score visualization (lead detail → scoring tab) */
.fit-viz { margin: var(--space-6) 0 var(--space-8); }
.fit-viz-header {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}
.fit-viz-tier {
  display: inline-block;
  width: 28px;
  height: 24px;
  line-height: 24px;
  text-align: center;
  font-family: var(--font-body);
  font-weight: 500;
  font-size: 13px;
  color: #FFFFFF;
  border-radius: 2px;
}
.fit-viz-tier.tier-a { background: var(--tier-a); }
.fit-viz-tier.tier-b { background: var(--tier-b); }
.fit-viz-tier.tier-c { background: var(--tier-c); }
.fit-viz-tier.tier-d { background: var(--tier-d); }
.fit-viz-tier.tier- {
  background: transparent;
  color: var(--ink-muted);
  border: 1px solid var(--hairline-strong);
  line-height: 22px;
}
.fit-viz-score {
  font-family: var(--font-display);
  font-size: 48px;
  font-weight: 400;
  font-variant-numeric: tabular-nums;
  font-variation-settings: 'opsz' 144, 'SOFT' 50, 'WONK' 1;
  letter-spacing: -0.02em;
  color: var(--ink);
  line-height: 1;
}
.fit-viz-suffix {
  font-family: var(--font-body);
  font-weight: 400;
  font-size: 14px;
  color: var(--ink-muted);
}
.fit-viz-track {
  position: relative;
  display: flex;
  height: 6px;
  border-radius: 2px;
  overflow: hidden;
  background: rgba(15, 14, 12, 0.06);
}
.fit-viz-band {
  display: block;
  height: 100%;
}
.fit-viz-band.band-d { width: 40%; background: rgba(142, 138, 131, 0.18); }
.fit-viz-band.band-c { width: 20%; background: rgba(110, 106, 99, 0.18); }
.fit-viz-band.band-b { width: 20%; background: rgba(168, 118, 46, 0.18); }
.fit-viz-band.band-a { width: 20%; background: rgba(31, 61, 229, 0.18); }
.fit-viz-fill {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  background: var(--cobalt);
  border-radius: 2px;
  transition: width 240ms ease-out;
}
.fit-viz-marker {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 12px;
  background: var(--cobalt);
  transform: translateX(-1px);
}
.fit-viz-scale {
  display: flex;
  justify-content: space-between;
  margin-top: var(--space-3);
  font-family: var(--font-mono);
  font-weight: 500;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  color: var(--ink-muted);
  letter-spacing: 0.1em;
}

/* Staggered page-load reveal */
@keyframes editorial-reveal {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

.hero-headline,
.hero-sublabel,
.fit-viz,
.kpi-card,
[data-testid="stMetric"] {
  animation: editorial-reveal 480ms cubic-bezier(0.22, 1, 0.36, 1) backwards;
}

.hero-headline { animation-delay: 0ms; }
.hero-sublabel { animation-delay: 80ms; }
.fit-viz       { animation-delay: 100ms; }

.kpi-card:nth-of-type(1),
[data-testid="stMetric"]:nth-of-type(1) { animation-delay: 160ms; }
.kpi-card:nth-of-type(2),
[data-testid="stMetric"]:nth-of-type(2) { animation-delay: 200ms; }
.kpi-card:nth-of-type(3),
[data-testid="stMetric"]:nth-of-type(3) { animation-delay: 240ms; }
.kpi-card:nth-of-type(4),
[data-testid="stMetric"]:nth-of-type(4) { animation-delay: 280ms; }
.kpi-card:nth-of-type(5),
[data-testid="stMetric"]:nth-of-type(5) { animation-delay: 320ms; }

@media (prefers-reduced-motion: reduce) {
  .hero-headline, .hero-sublabel, .fit-viz,
  .kpi-card, [data-testid="stMetric"] {
    animation: none !important;
  }
  .kpi-card:hover, .card:hover { transform: none; }
  .fit-viz-fill { transition: none; }
}

/* Tier pill — 24x20 colored square, white text */
.tier-pill {
  display: inline-block;
  width: 24px;
  height: 20px;
  line-height: 20px;
  text-align: center;
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0;
  color: #FFFFFF;
  border-radius: 2px;
  text-transform: uppercase;
  vertical-align: middle;
}
.tier-pill.tier-a { background: var(--tier-a); }
.tier-pill.tier-b { background: var(--tier-b); }
.tier-pill.tier-c { background: var(--tier-c); }
.tier-pill.tier-d { background: var(--tier-d); }
.tier-pill.tier-none {
  background: transparent;
  color: var(--ink-muted);
  border: 1px solid var(--hairline-strong);
  line-height: 18px;
}

/* ============================================================ */
/* 17b. KPI state accents (Phase 8f)                             */
/* ============================================================ */
.kpi-card {
  border-top: 2px solid var(--hairline);
}
.kpi-card.kpi-state-primary { border-top-color: var(--cobalt); }
.kpi-card.kpi-state-success { border-top-color: var(--color-success); }
.kpi-card.kpi-state-warning { border-top-color: var(--color-warning); }
.kpi-card.kpi-state-danger  { border-top-color: var(--color-error); }
.kpi-card.kpi-state-neutral { border-top-color: var(--hairline-strong); }

.kpi-card .kpi-label {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}
.kpi-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  font-size: 14px;
  line-height: 1;
  color: var(--ink-muted);
  letter-spacing: 0;
}
.kpi-icon-primary { color: var(--cobalt); }
.kpi-icon-success { color: var(--color-success); }
.kpi-icon-warning { color: var(--color-warning); }
.kpi-icon-danger  { color: var(--color-error); }

/* ============================================================ */
/* 17c. Leads-by-tier mini chart (Phase 8f)                      */
/* ============================================================ */
.leads-by-tier {
  padding: var(--space-8);
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--paper);
  box-shadow: inset 0 0 0 0.5px rgba(15, 14, 12, 0.04);
}
.ltc-title {
  font-family: var(--font-body);
  font-weight: 500;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--ink-muted);
  margin-bottom: var(--space-6);
}
.ltc-row {
  display: grid;
  grid-template-columns: 80px 1fr 50px;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.ltc-row:last-child { margin-bottom: 0; }
.ltc-label {
  font-family: var(--font-body);
  font-weight: 500;
  font-size: 13px;
  color: var(--ink);
}
.ltc-track {
  height: 8px;
  background: rgba(15, 14, 12, 0.05);
  border-radius: 2px;
  overflow: hidden;
}
.ltc-bar {
  height: 100%;
  border-radius: 2px;
  transition: width 400ms ease-out;
}
.ltc-bar.tier-a    { background: var(--tier-a); }
.ltc-bar.tier-b    { background: var(--tier-b); }
.ltc-bar.tier-c    { background: var(--tier-c); }
.ltc-bar.tier-d    { background: var(--tier-d); }
.ltc-bar.tier-none { background: rgba(15, 14, 12, 0.15); }
.ltc-count {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 14px;
  font-weight: 500;
  color: var(--ink);
  text-align: right;
}

/* ============================================================ */
/* 17d. Cobalt promo / CTA block (Phase 8f)                      */
/* ============================================================ */
.promo-block {
  background: var(--cobalt);
  color: #FFFFFF;
  padding: var(--space-12) var(--space-12);
  border-radius: 8px;
  margin: var(--space-8) 0 var(--space-4) 0;
}
.promo-headline {
  font-family: var(--font-display);
  font-variation-settings: 'opsz' 144, 'SOFT' 50, 'WONK' 1;
  font-size: 32px;
  font-weight: 400;
  line-height: 1.1;
  letter-spacing: -0.02em;
  color: #FFFFFF;
  margin-bottom: var(--space-3);
}
.promo-body {
  font-family: var(--font-body);
  font-weight: 400;
  font-size: 15px;
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.85);
  max-width: 520px;
  margin-bottom: 0;
}

/* Style the Streamlit button that follows a .promo-block in the same
   vertical block so the CTA reads as part of the cobalt panel. The
   .stElementContainer wrapping covers both legacy and new Streamlit DOM. */
.promo-block + div .stButton > button,
.promo-block ~ div .stButton > button[kind="primary"],
.promo-block + .stElementContainer .stButton > button {
  background: #FFFFFF !important;
  color: var(--cobalt) !important;
  border: 1px solid #FFFFFF !important;
  margin-top: calc(-1 * var(--space-6));
  margin-bottom: var(--space-4);
}
.promo-block + div .stButton > button:hover,
.promo-block ~ div .stButton > button[kind="primary"]:hover,
.promo-block + .stElementContainer .stButton > button:hover {
  background: rgba(255, 255, 255, 0.92) !important;
  color: var(--cobalt) !important;
  border-color: #FFFFFF !important;
}

/* ============================================================ */
/* 17e. Activity feed (Phase 8f)                                 */
/* ============================================================ */
.activity-feed {
  padding: var(--space-8);
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--paper);
  box-shadow: inset 0 0 0 0.5px rgba(15, 14, 12, 0.04);
}
.activity-title {
  font-family: var(--font-body);
  font-weight: 500;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--ink-muted);
  margin-bottom: var(--space-6);
}
.activity-row {
  display: grid;
  grid-template-columns: 10px 1fr 88px;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--hairline);
  font-family: var(--font-body);
  font-weight: 400;
  font-size: 14px;
  color: var(--ink);
}
.activity-row:last-child { border-bottom: none; }
.activity-text {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.activity-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.dot-primary { background: var(--cobalt); }
.dot-success { background: var(--color-success); }
.dot-warning { background: var(--color-warning); }
.dot-neutral { background: var(--ink-muted); }
.activity-when {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--ink-muted);
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.activity-empty {
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--ink-muted);
  padding: var(--space-4) 0;
}

/* ============================================================ */
/* 17g. Filter row card (Phase 8g)                               */
/* ============================================================ */
.filter-row {
  background: var(--paper);
  border: 1px solid var(--hairline);
  border-radius: 8px;
  padding: var(--space-6) var(--space-8);
  margin-bottom: var(--space-6);
  box-shadow: inset 0 0 0 0.5px rgba(15, 14, 12, 0.04);
}

/* ============================================================ */
/* 17f. Status pill (Phase 8f)                                   */
/* ============================================================ */
.status-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 2px;
  font-family: var(--font-body);
  font-weight: 500;
  font-size: 11px;
  color: #FFFFFF;
  letter-spacing: 0.04em;
  line-height: 16px;
  vertical-align: middle;
}

/* Streamlit dataframe-cell pills (`:color-background[...]` shorthand)
   need their own selector — they don't live inside stMarkdownContainer.
   Targets the rendered colour-background spans within data cells so the
   leads-table Status column matches the design palette. */
[data-testid="stDataFrame"] span[style*="background-color"] {
  font-family: var(--font-body) !important;
  font-size: 11px !important;
  font-weight: 500 !important;
  letter-spacing: 0.04em !important;
  padding: 2px 8px !important;
  border-radius: 2px !important;
  line-height: 16px !important;
  text-transform: none !important;
}
[data-testid="stDataFrame"] span[style*="rgb(33, 195, 84"],
[data-testid="stDataFrame"] span[style*="rgba(33, 195, 84"],
[data-testid="stDataFrame"] span[style*="rgb(212, 233, 217)"] {
  background: var(--color-success) !important; color: #FFFFFF !important;
}
[data-testid="stDataFrame"] span[style*="rgb(0, 104, 201"],
[data-testid="stDataFrame"] span[style*="rgba(0, 104, 201"],
[data-testid="stDataFrame"] span[style*="rgb(204, 224, 244)"],
[data-testid="stDataFrame"] span[style*="rgb(28, 131, 225)"] {
  background: var(--cobalt) !important; color: #FFFFFF !important;
}
[data-testid="stDataFrame"] span[style*="rgb(255, 137, 0"],
[data-testid="stDataFrame"] span[style*="rgba(255, 137, 0"],
[data-testid="stDataFrame"] span[style*="rgb(255, 226, 188)"] {
  background: var(--color-warning) !important; color: #FFFFFF !important;
}
[data-testid="stDataFrame"] span[style*="rgb(255, 43, 43"],
[data-testid="stDataFrame"] span[style*="rgba(255, 43, 43"],
[data-testid="stDataFrame"] span[style*="rgb(255, 219, 219)"] {
  background: var(--color-error) !important; color: #FFFFFF !important;
}
[data-testid="stDataFrame"] span[style*="rgb(155, 158, 161"],
[data-testid="stDataFrame"] span[style*="rgba(155, 158, 161"],
[data-testid="stDataFrame"] span[style*="rgb(225, 227, 229)"] {
  background: var(--ink-muted) !important; color: #FFFFFF !important;
}

/* ============================================================ */
/* 18. Misc                                                     */
/* ============================================================ */
.stCodeBlock, [data-testid="stCodeBlock"] pre {
  background: var(--paper-subtle) !important;
  border: 1px solid var(--hairline) !important;
  border-radius: 8px !important;
  font-family: var(--font-mono) !important;
  font-size: 13px !important;
  box-shadow: none !important;
}

[data-testid="stJson"] {
  background: var(--paper-subtle) !important;
  border: 1px solid var(--hairline) !important;
  border-radius: 8px !important;
  font-family: var(--font-mono) !important;
  box-shadow: none !important;
}

/* Nuke residual shadows on common Streamlit containers */
[data-testid="stVerticalBlock"],
[data-testid="stHorizontalBlock"],
[data-testid="stContainer"],
[data-testid="element-container"] {
  box-shadow: none !important;
}

@media (max-width: 900px) {
  [data-testid="stMain"] .block-container {
    padding-top: var(--space-6) !important;
    padding-bottom: var(--space-8) !important;
  }
  h1, .stMarkdown h1 { font-size: 36px !important; }
  h2, .stMarkdown h2 { font-size: 26px !important; }
}
</style>
"""


def inject_styles() -> None:
    """Inject the global stylesheet. Call at the top of every page."""
    st.markdown(_CSS, unsafe_allow_html=True)
