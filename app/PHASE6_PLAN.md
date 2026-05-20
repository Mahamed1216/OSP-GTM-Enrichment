# Phase 6 — ICP Configuration + Content Quality (Working Copy)

> **Status:** in flight. Authoritative plan lives at
> `~/.claude/plans/phase-6-fluffy-neumann.md` — this file is the in-repo
> working copy with sub-phase progress and tradeoff notes the user asked
> for in the Phase 6 spec.

## Context

The scoring rubric (`src/prompts/scoring.py:5-48`) and the email copywriter
prompt (`src/prompts/email.py:6-32`) bake in concrete assumptions about *what
we sell* and *who we target*. The Tavily industry-news query
(`src/enrichment/news.py:59`) is shallower still —
`f"{industry} industry trends 2026"` — which pulls news that rarely maps to
our value prop.

Phase 6 externalises that configuration into a single user-editable JSON
file surfaced through a Streamlit Settings page. The same config feeds
scoring, three content generators, both Tavily queries, and a new
"generate content for all tiers" demo toggle. Defaults match today's
hardcoded text verbatim so behaviour pre-first-edit is identical to today.

## Locked Decisions

1. **Storage:** `data/icp_config.json` singleton, reusing the on-disk
   pattern from `data/winning_examples.json`. No DB.
2. **Schema:** Pydantic `BaseModel` (matches existing convention in
   `src/config.py` and `src/enrichment/schemas.py`). Free
   `model_dump_json(indent=2)` produces the pretty-printed format the
   spec asks for.
3. **Module location:** `src/icp_config.py` (flat, sibling to
   `src/config.py`) rather than promoting `src/config.py` to a package —
   purely additive.
4. **Concurrency:** atomic write (`tmp` + `os.replace`) on save, plus
   snapshot-per-pipeline-run on load. A save during a multi-lead run
   takes effect on the next lead, never mid-lead.
5. **Override scope:** `generate_content_for_all_tiers=True` only unlocks
   content generation; delivery is still gated by `SEND_MIN_TIER`.
   *(answered during planning)*
6. **Disqualifier gate:** deferred this phase. Disqualifiers appear in the
   prompt as "who NOT to target"; no Python pre-gen gate.
   *(answered during planning)*
7. **Tavily composition:** company news anchors on the company name with
   ICP terms as qualifier; industry news drops the lead's `industry` and
   uses the ICP's own `news_search_terms` (with the lead's industry as
   fallback when terms are empty).
8. **ICP block placement:** appended to existing `SYSTEM` constants via a
   `build_system(...)` helper. The constants themselves stay byte-for-byte
   identical, preserving `PROMPT_VERSION` provenance.

## Sub-phase Progress

- [x] **6a — Backend foundation**
  - `src/icp_config.py` (models + load/save/default/render, atomic write)
  - `data/icp_config.json` (verbatim seed)
  - `tests/test_icp_config.py` (defaults, missing-file fallback,
    round-trip, atomic-write cleanup, render snapshot)
- [x] **6b — Scoring + Content prompt integration**
  - `build_system` extended on all 4 prompts to accept optional `icp`
  - `src/scoring.py` + 3 `src/content/*.py` modules load the snapshot
  - Live LLM run on Eric Gordon (lead 9): score 25/C with sharper
    "competitor not buyer" rationale; email/call/DM all reference
    specific signals.
  - **Post-mortem finding:** rendered prompt still has ICP-flavored
    text in *both* SYSTEM constant *and* the appended ICP block —
    invisible while defaults match, but will produce conflicting
    signals once the user edits Settings. Refactor folded into 6d.1.
- [x] **6c — Tavily query refinement**
  - `fetch_company_news` / `fetch_industry_news` accept optional `icp` kwarg
  - `waterfall.py` loads ICP once, threads to both fetchers
  - Live run: Eric's enrichment now surfaces the Parakeet AJ Kissh
    promotion in both feeds (real value-prop hit). Residual noise
    (video games, earnings reports) tracked in
    `phase7_tavily_refinement` memory.
- [x] **6d — SYSTEM constant refactor + Settings page UI**
  - 6d.1 stripped hardcoded ICP refs from `SYSTEM` in all four prompts;
    bumped `PROMPT_VERSION` v1 → v2.
  - 6d.2 `app/pages/6_settings.py` with five forms (Company, ICP,
    Persona, Signals, News terms) + Demo toggle + two-click reset.
    Registered in `app/main.py` between Run Pipeline and Engagement.
  - Verification: AppTest drove the UI to change `company.one_liner`
    to "AI-native lead-research copilot for outbound teams"; disk
    re-read confirmed the write. Eric regenerated end-to-end with
    the new one-liner: score dropped 25 → 18 with explicit
    "competitor / disqualifier" framing; email/call/DM all
    acknowledge the peer-not-prospect dynamic. Duplication audit
    after refactor: `wasted pipeline effort` 0 occurrences (was in
    SYSTEM); `B2B SaaS company that helps` 0 occurrences. ICP config
    reset to seeded defaults before moving on.
- [x] **6e — Tier override + pipeline integration**
  - `src/pipeline.py` loads ICP once at top of `run_pipeline_for_lead`
    for the override boolean; downstream functions keep their per-call
    loads (see plan tradeoff (c)).
  - Gate rewritten: `tier_gate_open = icp.generate_content_for_all_tiers
    or settings.should_send(tier)`. Delivery stays gated on
    `should_send` only — override never causes a send.
  - New log line `pipeline_tier_override_active` for observability.
  - Live verification on lead 9 with `send_min_tier="B"` monkeypatched
    (the user's `.env` has `SEND_MIN_TIER=C` so the toggle is a no-op
    against current config; report flags this honestly):
    - Toggle OFF → `content_skipped=True`, all content fields None,
      delivery None.
    - Toggle ON  → content fields populated, delivery still None.
  - DM 280-char drift retired: 3 post-v2 samples (218 / 209 / 254
    chars) all under 280.

## Tradeoffs Captured (per spec checklist)

### (a) JSON file vs. DB

JSON wins because:
- Zero migration cost.
- Human-readable diffs (`git diff data/icp_config.json` tells the whole
  story).
- Copy-paste portability between checkouts and demos.
- Reuses the proven loader pattern from
  `src/content/winners.py:61-69`.

Costs:
- No history — a save overwrites prior config. Git tracks it if the user
  commits, which they should.
- Single-tenant. Fine: this tool is single-SDR by design
  (see `rater_id` hardcoded in `src/config.py:46`).

### (b) Tavily query composition from `news_search_terms`

```python
def _company_news_query(company: str, icp: ICPConfig) -> str:
    qualifier = " ".join(icp.news_search_terms[:2])
    if qualifier:
        return f'"{company}" {qualifier}'
    return f'"{company}" news OR funding OR launch OR hire'

def _industry_news_query(industry: str, icp: ICPConfig) -> str:
    terms = " ".join(icp.news_search_terms)
    if terms:
        return f"{terms} 2026"
    return f"{industry} industry trends 2026"
```

Both functions accept `icp: ICPConfig | None = None` so legacy callers
(tests, scripts) keep their existing query exactly. The lead's `industry`
remains the fallback when `news_search_terms` is empty — preserving
today's behaviour for an unconfigured install.

### (c) Mid-edit during pipeline run

Atomic file replace on save + snapshot-per-pipeline-run on load:

- `save_icp_config(cfg)` writes `data/icp_config.json.tmp` then
  `os.replace(tmp, final)` — atomic on Windows and POSIX. A concurrent
  reader sees either the old file or the new one, never partial bytes.
- `run_pipeline_for_lead` calls `load_icp_config()` exactly once at the
  top and threads the resulting snapshot through scoring → content →
  news. A save that lands mid-batch takes effect on the next lead.

Trade: freshness for consistency. The right call for prompt material —
batch-mate emails should cite the same value prop.

## Verification (end-to-end after 6e)

1. `python -m pytest` — all tests green.
2. `streamlit run app/main.py` → Settings → edit one-liner → save →
   reload → confirm persisted → "Reset to defaults" two-click.
3. Re-enrich + re-score + re-content Eric Gordon with edited one-liner →
   confirm generated email reflects the edit.
4. Delete `data/icp_config.json` → reload → confirm fallback to defaults
   keeps Settings + pipeline working.
5. Toggle `generate_content_for_all_tiers=True` → run a Tier C lead →
   confirm content generated, delivery skipped, override log emitted.
