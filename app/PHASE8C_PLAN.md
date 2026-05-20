# Phase 8c — Visual polish (in-repo design doc)

## Why

Default Streamlit screams "Streamlit." For a take-home judged on
product quality, the demo needs an aesthetic point of view that
reviewers register *before* they read the code.

Polish only — no layout changes, no widget functional changes. Fonts,
color, spacing, badge styling, KPI cards, button hierarchy, sidebar
treatment. Pass criterion: a colleague glancing at a screenshot should
not say "that's Streamlit."

## Palette

| Role | Value | Notes |
|---|---|---|
| Accent | `#1F3DE5` (deep cobalt) | Used sparingly — primary buttons, Tier A pills, active sidebar marker, focus rings, KPI underline accents. Picked over generic Linear-blue/Stripe-purple by going deeper-saturated and pairing with editorial serif. |
| Background | `#FAF8F4` (warm paper) | Off-white, not pure white. Warm-on-cool tension with the cobalt. |
| Foreground | `#0F0E0C` | Soft black, never `#000`. |
| Muted | `#6B6660` | Captions, small-caps labels, secondary text. |
| Hairline | `rgba(15, 14, 12, 0.10)` | All borders are 1px hairline. No shadows anywhere. |
| Accent tint | `rgba(31, 61, 229, 0.10)` | Tier B pill, secondary button hover, expander expanded-state header. |
| Subtle paper | `#F2EFE9` | Streamlit's secondary background (sidebar fallback, container fills). |
| Neutral chip | `#E8E5DE` | Tier C pill background. |

## Type system

| Family | Source | Use |
|---|---|---|
| **Fraunces** (variable: opsz, wght, SOFT, WONK) | Google Fonts | Display — page titles, KPI "story" numbers like reply-rate %. Axes locked to `opsz=144 SOFT=50 WONK=1` for a sharp editorial cut, not the default soft serif feel. |
| **Geist** (variable: wght 100–900) | Google Fonts | Body, captions, form labels, button text, table headers (small-caps treatment via `text-transform: uppercase; letter-spacing: 0.06em–0.08em`). |
| **JetBrains Mono** (variable: wght 100–800) | Google Fonts | Tabular KPI numbers, tier pills, lead IDs, code blocks. Always with `font-variant-numeric: tabular-nums`. |

Loaded via `@import url(...)` at the top of the injected CSS so a
single fetch covers all weights/axes.

## Visual hierarchy decisions

- **Page title (st.title)**: Fraunces 44px, opsz=144, weight 500, foreground.
- **Section header (st.header / st.subheader)**: Geist 18px, weight 600, foreground, with a 24px top margin and a 1px hairline 12px below.
- **Caption (st.caption)**: Geist 13px, muted color, line-height 1.5.
- **Body**: Geist 14–15px, foreground, line-height 1.6.
- **KPI label**: Geist 11px, uppercase, `letter-spacing: 0.08em`, muted.
- **KPI value (mono)**: JetBrains Mono 36px, weight 500, foreground, tabular-nums.
- **KPI value (serif)**: Fraunces 44px, weight 500, opsz=144, foreground — for "story" numbers (e.g., headline reply-rate).
- **Tier pill**: JetBrains Mono 11px bold; 4 variants (A solid cobalt, B accent-tint, C neutral chip, D outlined).
- **Buttons**: 6px radius, 0.6rem × 1.2rem padding, no shadow. Primary solid cobalt; secondary cobalt-outline; ghost text-only.
- **Hairlines, no shadows**: depth comes from typography hierarchy and color, never from elevation.

## CSS section list (sections in the single injected `<style>` block)

1. `@import` Google Fonts (Fraunces, Geist, JetBrains Mono — all variable).
2. `:root` CSS custom properties for all colors + fonts.
3. Base reset: `body`, `html`, `[data-testid="stAppViewContainer"]` — paper background, Geist body font, foreground text.
4. Typography: `h1`–`h4` rules with Fraunces + variation settings for h1; Geist with weight contrast for h2–h4.
5. Layout: increase main-canvas vertical padding for breathing room; subtle increase to central column max-width.
6. Sidebar (`[data-testid="stSidebar"]`): 1px right hairline, `#F4F1EB` background.
7. Sidebar nav links: Geist 13px, `letter-spacing: 0.02em`, active link gets a 3px cobalt left bar (no full-width fill).
8. Primary button (`[data-testid="baseButton-primary"]`): solid cobalt, paper text, hover darkens.
9. Secondary button (`[data-testid="baseButton-secondary"]`): cobalt outline, hover fills with accent-tint.
10. Form submit buttons: same treatment as primary/secondary.
11. Inputs (`stTextInput`, `stTextArea`, `stSelectbox`, `stNumberInput`, `stMultiSelect`, `stFileUploader`): hairline borders, cobalt focus ring via `outline`.
12. Toggle / checkbox: cobalt fill when active.
13. `st.metric` (`[data-testid="stMetric"]`): restyle for the few places we keep native metric — strip chrome, Fraunces value, uppercase Geist label.
14. `st.dataframe` (`[data-testid="stDataFrame"]`): column headers uppercase Geist 12px muted; row hover hairline.
15. `st.tabs` (`[data-testid="stTabs"]`): Geist 13px tab labels, active tab gets 2px cobalt underline.
16. `st.expander` (`[data-testid="stExpander"]`): hairline border, muted chevron, expanded-state subtle accent-tint header.
17. `st.status` (`[data-testid="stStatusWidget"]`): cobalt running state, muted complete state.
18. Alerts (`[data-testid="stAlert"]` with kind variants): hairline borders, accent-tint backgrounds instead of saturated color blocks.
19. `st.divider`: 1px hairline at 60% opacity, generous margin.
20. Captured Streamlit markdown background-spans (`stMarkdownContainer span[style*="background"]`): restyle to unified tier-pill aesthetic.
21. Custom classes: `.kpi-card`, `.kpi-label`, `.kpi-value-mono`, `.kpi-value-serif`, `.kpi-sublabel`, `.tier-pill` + `.tier-a/b/c/d`, `.section-heading`.
22. Responsive guard: `@media (max-width: 900px)` minor adjustments.

## Files touched

| File | Change |
|---|---|
| `app/styles.py` | NEW — `inject_styles()` emitting the full `<style>` block. |
| `app/lib/components.py` | NEW — `kpi_card(label, value, sublabel, numeric_font)`. |
| `app/lib/badges.py` | EXTEND — add `tier_pill_html(tier)`. Keep `tier_badge()` for dataframe cells. |
| `.streamlit/config.toml` | EXTEND — `[theme]` block with cobalt + paper colors so native fallbacks match. |
| `app/main.py` | EXTEND — `inject_styles()` after `set_page_config`. |
| `app/pages/1_dashboard.py` | EXTEND — top-row metrics → `kpi_card`. |
| `app/pages/2_leads.py` | EXTEND — `inject_styles()` only (CSS-driven restyle). |
| `app/pages/3_lead_detail.py` | EXTEND — `inject_styles()` only. |
| `app/pages/4_run_pipeline.py` | EXTEND — `inject_styles()` only. |
| `app/pages/5_engagement.py` | EXTEND — top-row metrics → `kpi_card`. |
| `app/pages/6_settings.py` | EXTEND — `inject_styles()` only. |

## What stays the same

- Every widget's position and behavior — no layout changes.
- All 128 tests continue to pass (none touch the UI layer).
- `tier_badge()` for `st.dataframe` cells — Streamlit doesn't render
  HTML in cells, so the existing `:color-background[]` markdown stays
  and gets CSS-restyled at the rendered span level.
- Functional emoji (✅/❌, ⚠️) on status indicators — those are
  micro-icons, not branding surface.
- Sidebar navigation order, page icons, page titles.

## Pass criterion

Screenshots of all 8 pages should be unrecognizable as default
Streamlit. The three immediately visible non-defaults that flag
"intentional design":

1. **Cobalt** — primary buttons, active sidebar bar, Tier A pills.
2. **Fraunces** — every page title and the headline KPI numbers.
3. **Paper background + hairlines** — replaces pure white + heavy
   chrome.
