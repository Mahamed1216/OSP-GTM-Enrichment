# Phase 7 — Engagement page (Working Copy)

> Authoritative plan: `~/.claude/plans/phase-6-fluffy-neumann.md`
> (named for the prior phase's slot; the contents are Phase 7's plan).
> This file is the in-repo working copy with sub-phase progress.

## Context

The Engagement page in the sidebar (`app/pages/5_engagement.py`) is a
six-line placeholder. The phase-5/6 *backend* work landed
(`src/feedback/engagement.py`, `src/feedback/learning.py`, the
Engagement table, `last_sync_time` and `reply_rate_series` query
helpers) but the UI never got built. Phase 7 wires it together.

Sections, top to bottom:
1. Sync controls (sync engagement; promote winners)
2. KPI row (sent / reply rate / open rate / bounce rate)
3. Reply rate over time chart (`reply_rate_series`)
4. Winners library (active + demoted, filterable)
5. Negative examples library (same shape)
6. Recent engagement events

Schema-level change: add `is_active` field to winners/negatives JSON
files so the SDR can demote bad picks without losing them from history.

## Locked Decisions

1. **Rewrite in place at `app/pages/5_engagement.py`** — smallest diff;
   `app/main.py` and the `/engagement` URL stay unchanged.
2. **`is_active` stamped at both ends** — new entries from
   `src/feedback/learning.py` get `is_active=True`; loaders in
   `src/content/winners.py` filter on `e.get("is_active", True)` so
   pre-migration entries are treated as active by default.
3. **Demote helper lives in `src/content/winners.py`** — atomic write
   via `tmp` + `os.replace`, same pattern as `src/icp_config.py`.
4. **Migration script `scripts/migrate_is_active.py`** — idempotent,
   modeled on `scripts/migrate_winning_examples.py`.
5. **No new engagement DB column** — table already has every column
   we need.
6. **"Promote winners now" button calls BOTH** `promote_winners()`
   and `process_ratings()` sequentially.
7. **Recent events: "Show last N" number_input**, not pagination.

## Sub-phase Progress

- [x] **7a — Skeleton, sync controls, KPIs**
  - Placeholder replaced with scaffold
  - `kpi_counts()` extended with `opened` / `clicked` / `bounced`
  - Sync buttons wired with spinner / toast / cache clear
- [x] **7b — Reply-rate chart + library tables**
  - `st.line_chart(reply_rate_series(days))` with days input
  - `list_all_winners` / `list_all_negatives` helpers
  - Both library tables with content-type filter + row expansion
- [x] **7c — Demote + recent events + migration**
  - `is_active` filter in `load_top_winners_for` / `load_top_negatives`
  - `set_winner_active` / `set_negative_active` (atomic write)
  - `is_active: True` stamped on new entries by all three `_make_*`
    helpers in `src/feedback/learning.py`
  - `recent_engagement(limit)` in `app/lib/db_queries.py`
  - Two-click TTL demote button on selected library row;
    "Restore" button when already inactive
  - `scripts/migrate_is_active.py` (idempotent)
  - 9 new tests; **104 total green** (up from 95)
  - **E2E verified:** demoting `seed_001` removes
    `"About that CRM data hygiene post"` from the rendered email
    few-shot prompt; restoring brings it back.

## Files To Be Modified

| Path | Notes |
|---|---|
| `app/pages/5_engagement.py` | placeholder → full page |
| `app/lib/db_queries.py` | extend `kpi_counts`, add `recent_engagement` |
| `src/content/winners.py` | `is_active` filter + demote helpers + `list_all_*` |
| `src/feedback/learning.py` | stamp `is_active: True` on new entries |
| `scripts/migrate_is_active.py` | one-off idempotent backfill (NEW) |
| `tests/test_winners_loader.py` | loader filter tests |
| `tests/test_learning_v2.py` | new-entry stamping tests |

## Existing Functions Used (reused, not reimplemented)

- `sync_engagement()` — `src/feedback/engagement.py:56`
- `promote_winners()` — `src/feedback/learning.py:90`
- `process_ratings()` — `src/feedback/learning.py:180`
- `last_sync_time()` — `app/lib/db_queries.py:273`
- `reply_rate_series(days)` — `app/lib/db_queries.py:279`
- Two-click confirm pattern — `app/pages/3_lead_detail.py:468-533`
- Atomic JSON write pattern — `src/icp_config.py:save_icp_config`

## Verification (end-to-end after 7c)

1. `python -m pytest --ignore=tests/test_llm_live.py` — 95+ tests green
2. `python scripts/migrate_is_active.py` — idempotent on rerun
3. `streamlit run app/main.py` → Engagement page renders all sections
4. Sync button — toast, timestamp updates, KPIs refresh
5. Demote a winner — JSON shows `is_active: false`, regenerating an
   email proves the demoted entry no longer appears in the few-shot
   prompt
