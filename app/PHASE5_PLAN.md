# Phase 5 — Manual SDR Rating + Self-Improvement: Plan

## Context

Phases 1–4 shipped a working UI on top of an unmodified backend. Phase 5 adds a closed-loop content quality system: SDRs thumbs-up/down generated content; ratings feed two few-shot libraries (winners + negatives); regenerated content is versioned through a `superseded_by_id` chain.

This phase **does** modify `src/`, but additively only — new modules, new functions, additive kwargs, new DB columns. Existing function signatures, schemas, and the 21 passing tests must stay intact. If any sub-phase tempts a refactor, I will stop and ask.

---

## Tradeoffs flagged for your call

### T1. `winning_examples.json` migration: hard rewrite vs. additive overlay

The existing schema is `{id, lead_context, subject, body, reply_rate, manually_flagged, notes, [promoted_at]}`. The spec's new schema is `{content_type, content: {subject, body}, lead_context_summary, source, score, added_at}` — a substantially different shape. A hard rewrite would break `format_winners()` (it reads `subject`/`body` at the top level) and force changes to existing test fixtures.

**Recommended: additive overlay.** Migration script *adds* new fields (`content_type: "email"`, `source: "engagement_reply"`, `score: reply_rate`, `added_at: promoted_at or now`, `content: {subject, body}`) but keeps the existing top-level `subject`, `body`, `lead_context`, `reply_rate`, `manually_flagged`, `notes`. Existing code paths see a normal entry with extras; new code paths see the new fields. Idempotent: re-running the script on already-migrated entries is a no-op.

This has one consequence worth knowing: entries always carry both shapes. Slightly larger files, no functional cost, zero test risk.

### T2. Few-shot loader split (avoid touching `format_winners`)

`format_winners()` is exercised by `test_format_winners_includes_signal_in_output` and is hardcoded to email shape. Rather than overload it for call_script and linkedin_msg, I'll add **new sibling functions** in `src/content/winners.py`:

- `load_top_winners_for(content_type: str, k: int = 3) -> list[dict]`  — filters the same JSON file by `content_type`.
- `format_winners_for(content_type: str, winners: list[dict]) -> str` — content-type-aware formatter.
- `load_top_negatives(content_type: str, k: int = 2) -> list[dict]`
- `format_negatives(content_type: str, negatives: list[dict]) -> str` — applies the anti-pattern framing.

The existing `load_top_winners()` / `format_winners()` stay byte-for-byte unchanged. `src/content/email.py` will switch to the new helpers (a fresh import, not a rename) so it picks up negatives too. `src/content/call_script.py` and `src/content/linkedin_msg.py` will start using winners + negatives where they previously used neither.

### T3. Versioning UX in Lead Detail (which version do we show?)

Each `GeneratedContent` row may eventually have a `superseded_by_id` pointer to a newer version. Three open questions:

- **Default view**: show the newest version per kind (the row whose `superseded_by_id IS NULL`). If multiple rows exist for a kind, the head of the chain is the one to render.
- **Rating + regenerate are only on the head**: older versions are read-only with a banner "⚠️ Superseded by a newer version generated on <date>. [Show current]".
- **Older versions visible but folded**: below the head's content, render an `st.expander("📜 N earlier version(s)")` listing timestamps. Clicking one swaps the body view to that older version (per-kind session_state key tracks which is showing).

**Recommended**: head by default, expander reveals chain, banner on older versions. Rating widgets disabled outside the head. Cycle prevention: regenerate refuses if the source row already has `superseded_by_id` set.

### T4. How does `regenerate_with_feedback` know the new row's id?

The existing content generators return Pydantic schemas, not the inserted row's id. To set `old.superseded_by_id = new.id`, I need that id. Three options:

- (a) Change generator return types to `(schema, content_id)` — additive but breaks `pipeline.py`'s tuple-unpack expectations.
- (b) Add a new "with-id" wrapper (`generate_email_with_id(...) -> tuple[EmailResult, int]`).
- (c) After calling the generator, look up the newest `GeneratedContent` row for that `(lead_id, kind)`.

**Recommended: (c).** Streamlit is single-threaded per session and the demo doesn't hit this path concurrently. The newest row by `created_at desc` for that `(lead_id, kind)` is deterministic enough. Zero touch on existing return types and zero touch on `pipeline.py`. If we ever go multi-tenant/concurrent, we revisit.

### T5. `process_ratings` idempotency mechanism

Need to avoid duplicate winner/negative entries on re-runs. Three options:

- (a) Add a `processed_at` column to `ContentRating`.
- (b) High-water-mark stored in a separate state file.
- (c) Embed `rating_id` inside each library entry and skip on next run if seen — mirrors how `promote_winners` already uses `auto_{content_id}`.

**Recommended: (c).** No new schema, no new state file, exactly the pattern already in use.

### T6. Migrated seed entries source label

Existing seed entries (`seed_001`–`seed_003`) have `manually_flagged: true` and weren't actually engagement-reply winners. The spec says "assume existing entries came from email engagement", so I'll tag them `source: "engagement_reply"` per spec. We keep `manually_flagged` as-is (separate concept).

---

## Database changes

### New table: `content_ratings`
```python
class ContentRating(Base):
    __tablename__ = "content_ratings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generated_content_id: Mapped[int] = mapped_column(
        ForeignKey("generated_contents.id"), nullable=False, index=True
    )
    rating: Mapped[str] = mapped_column(String(8), nullable=False)  # "up" | "down"
    feedback_text: Mapped[Optional[str]] = mapped_column(Text)
    rated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    rated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)

    generated_content: Mapped["GeneratedContent"] = relationship(back_populates="ratings")
```
Note: spec says `generated_content` table; the actual table is `generated_contents` (plural). FK target updated accordingly.

### Extension to `GeneratedContent`
Additive, both nullable:
- `superseded_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("generated_contents.id"), nullable=True)`
- `ratings: Mapped[list["ContentRating"]] = relationship(back_populates="generated_content", cascade="all, delete-orphan")`

### Migration script: `scripts/migrate_phase5.py`
1. `Base.metadata.create_all(engine)` — picks up the new `content_ratings` table.
2. Raw `ALTER TABLE generated_contents ADD COLUMN superseded_by_id INTEGER REFERENCES generated_contents(id)` guarded by a `PRAGMA table_info` check so it's idempotent.
3. Logs counts (table created? column added? both already present?).

---

## File-format changes

### `data/winning_examples.json` — additive overlay
Each entry gains: `content_type`, `source`, `score`, `added_at`, `content: {subject, body}`. Existing fields preserved. Rating-derived entries also include `rating_id`.

### `data/negative_examples.json` — new file
Initialized as `[]`. Entries: `{rating_id, content_type, content: {subject, body}, lead_context_summary, source: "manual_rating", feedback_reason, added_at}`. Cap at 25 (matches `MAX_LIBRARY_SIZE`).

### Migration scripts:
- `scripts/migrate_winning_examples.py` — idempotent overlay; checks for `content_type` key on each entry to decide whether to migrate.
- `scripts/migrate_negative_examples.py` — creates the empty file if missing (one-liner; could be folded into the winners migration but kept separate for clarity).

---

## New backend modules

### `src/feedback/ratings.py`
```python
async def record_rating(generated_content_id: int, rating: str,
                        feedback_text: str | None,
                        rated_by: str | None = None) -> ContentRating
def get_rating(generated_content_id: int) -> ContentRating | None
def get_unrated_content(content_type: str | None = None,
                        tier: str | None = None,
                        limit: int = 50) -> list[GeneratedContent]
def get_rating_trends(days: int = 30) -> dict[str, list[dict]]
```
- `record_rating` is async for symmetry with the rest of `src/feedback/*`, even though no I/O is async — keeps the boundary consistent.
- Validates rating ∈ {"up","down"}; rejects double-ratings on the same content (one rating per content row; subsequent rate calls update or raise — see Open Q1).

### `src/feedback/regenerate.py`
```python
async def regenerate_with_feedback(generated_content_id: int) -> int  # new content_id
```
1. Loads `GeneratedContent` by id; raise if `superseded_by_id IS NOT NULL` (T3 cycle guard).
2. Looks up its latest thumbs-down rating with non-empty `feedback_text`; raise if none.
3. Dispatches to the right generator by `kind` with `regeneration_feedback=feedback_text`.
4. Looks up the new row (T4 strategy: newest `(lead_id, kind)` by `created_at desc`).
5. Sets `old.superseded_by_id = new.id`; commits; returns `new.id`.

### `src/feedback/learning.py` — extension
- Existing `promote_winners()` keeps its behavior, but the new entries it writes via `_make_winner` gain `content_type: "email"`, `source: "engagement_reply"`, `score: reply_rate`, `added_at: promoted_at`, `content: {"subject":..., "body":...}` (overlay applied at write time too — keeps file consistent with migration output).
- New function: `process_ratings() -> dict`. Reads all `ContentRating` rows, dedupes via `rating_id` field embedded in library entries (T5). Up → winners; down with feedback → negatives. Both libraries trimmed to `MAX_LIBRARY_SIZE`. Returns `{processed: int, new_winners: int, new_negatives: int, winners_total: int, negatives_total: int}`.

### `src/prompts/email.py`, `src/prompts/call_script.py`, `src/prompts/linkedin_msg.py`
- Add a small Python helper section at module bottom: `def build_system(winners: list[dict], negatives: list[dict]) -> str` returns `SYSTEM` plus optionally appended winners block (existing) plus optionally appended negatives block. The hardcoded `SYSTEM` string itself stays unchanged so test fixtures and re-imports don't churn.

### `src/content/{email,call_script,linkedin_msg}.py` — additive kwarg
Each `generate_*(lead_id)` gains `*, regeneration_feedback: str | None = None`. When present, prepend a system block ahead of the few-shot:
```
"Previous version of this content was rated negatively. Reason: {feedback_text}.
Address this in your output."
```
All three modules also switch to the new winners loader (`load_top_winners_for(content_type, 3)`) and add negatives (`load_top_negatives(content_type, 2)`). Default behavior when libraries are empty is identical to today.

### `src/config.py` — additive setting
```python
rater_id: str = "demo_sdr"
```
Read by the UI (and `record_rating` if no explicit `rated_by` passed).

### `scripts/process_ratings.py`
Thin CLI that imports `learning.process_ratings()` and prints the summary. Mirrors `pull_engagement.py`. README gets a note placing it next to `pull_engagement.py` in the cron section.

---

## UI changes

### `app/lib/db_queries.py` — additions
- `list_unrated_content(content_type: str | None, tier: str | None, limit: int) -> pd.DataFrame` — joins `GeneratedContent` LEFT JOIN `ContentRating` IS NULL, filtered to non-superseded rows (`superseded_by_id IS NULL`). Returns: `content_id, lead_id, lead_name, company, content_type, tier, created_at`. Sorted by Tier A→B→C→null, then `created_at` ascending.
- `rating_summary_per_content_type(days: int = 30) -> pd.DataFrame` — for the Dashboard chart. Columns: `date, content_type, up_rate`.
- `get_content_with_rating(content_id: int) -> dict` — single row with rating + supersedes_chain hints. Used by Lead Detail to render the version block efficiently.

### `app/lib/rating_runner.py` — new
```python
def record_rating_sync(content_id: int, rating: str, feedback: str | None) -> None
def regenerate_sync(content_id: int) -> int   # returns new content_id
```
Both wrap the async backend functions via `app.lib.async_runner.run_async`.

### `app/pages/3_lead_detail.py` — extend Generated Content tab
For each kind sub-tab:
1. Determine the head row (newest, `superseded_by_id IS NULL`) and the chain of older rows.
2. Default render: head row body (existing rendering, no UI churn).
3. Below body, add a `st.container(border=True)` rating block:
   - **If unrated**: thumbs-up button, thumbs-down button, optional `st.text_input("Why? (optional)", key=f"ld_fb_{content_id}")`, "Submit rating" button. Submit calls `record_rating_sync` → `st.toast` → `st.rerun()`.
   - **If rated**: badge "👍 Rated by demo_sdr · <date>" or "👎 …" with feedback text below if present.
   - **If thumbs-down with non-empty feedback**: "🔄 Regenerate with feedback" button → `st.spinner("Regenerating…")` → `regenerate_sync` → `st.toast` → `st.rerun()`. Wraps in try/except → `st.error`.
4. If the chain has older rows, render `st.expander("📜 N earlier version(s)")` listing timestamps + "View this version" buttons. Selecting one swaps the displayed body to that older row (session-state key `ld_version_{lead_id}_{kind}` holds the active row id; default = head). When viewing an older version, banner reads "⚠️ Superseded by version generated on <date>. [Show current]" and the rating block is hidden.
5. Widget-key scoping: every key includes the content_id (or chain-active id) so two sub-tabs cannot collide.

### `app/pages/1_dashboard.py` — additions
Below existing content:
- **Review queue** (`st.subheader("Awaiting your review")`): caption with count, content_type multiselect filter (key `dash_review_filter_kind`), `st.dataframe` with `selection_mode="single-row"`. Row click writes `selected_lead_id` and `st.switch_page("pages/3_lead_detail.py")`.
- **Rating trends (last 30 days)**: pivoted DataFrame indexed by date with one column per content_type containing daily up-rate. `st.line_chart`. If <2 days of data, replaced by an info hint ("Trend chart appears once you have rated content across multiple days").

---

## Test strategy

New tests under `tests/`:
- `test_ratings.py` — record/get rating, get_unrated_content filters, double-rate behavior (per Open Q1), `get_rating_trends` returns expected shape.
- `test_learning_v2.py` — `process_ratings` idempotency, dedup via `rating_id`, MAX_LIBRARY_SIZE trimming, source tagging on engagement vs manual entries.
- `test_winners_loader.py` — `load_top_winners_for` filters by content_type, `format_negatives` uses anti-pattern framing string.
- `test_regenerate.py` — happy path (uses monkeypatched generator, no live API), rejects when source already superseded, rejects when no thumbs-down rating with feedback exists.
- `test_migrate_phase5.py` — idempotent migration, columns/tables present after second run.

Existing 21 tests untouched. Re-run before and after each sub-phase as a guard.

---

## Build sub-phases

Each sub-phase ends with: existing 21 tests green + new tests for that slice green + summarized to you for review before proceeding.

### 5a — Backend foundation
1. Add `ContentRating` model + extend `GeneratedContent` with `superseded_by_id` and `ratings` relationship.
2. `scripts/migrate_phase5.py` (idempotent).
3. `src/config.py`: add `rater_id`.
4. `src/feedback/ratings.py` with all four functions.
5. Tests: `test_ratings.py`, `test_migrate_phase5.py`.
6. Verification: run migration twice, insert a rating manually, query it back; existing 21 tests + new tests green.

### 5b — Learning extension
1. `data/winning_examples.json` migration via `scripts/migrate_winning_examples.py` (idempotent).
2. `data/negative_examples.json` initialized empty.
3. Extend `src/content/winners.py` with the four new helpers (`load_top_winners_for`, `format_winners_for`, `load_top_negatives`, `format_negatives`). Existing two helpers untouched.
4. Extend `src/prompts/{email,call_script,linkedin_msg}.py` with `build_system` helper.
5. Extend `src/content/{email,call_script,linkedin_msg}.py`: add `regeneration_feedback` kwarg; switch to new loaders.
6. Extend `src/feedback/learning.py`: enrich `_make_winner` output with new keys; add `process_ratings()`.
7. `scripts/process_ratings.py`.
8. Tests: `test_learning_v2.py`, `test_winners_loader.py`.
9. Verification: thumbs-up an existing content row in the DB manually, run `python scripts/process_ratings.py`, confirm winner appears in `winning_examples.json` with `source="manual_rating"` and `rating_id` embedded; existing tests green.

### 5c — Lead Detail rating UI (no regenerate)
1. `app/lib/rating_runner.py` (record only; regenerate left as a stub that raises `NotImplementedError`).
2. Extend `app/pages/3_lead_detail.py` Generated Content tab with rating widget (head version only). Older-version chain UI deferred to 5d.
3. Verification: thumbs-up a real content row → row in `content_ratings` → page re-renders showing "Rated by demo_sdr". Existing tests green.

### 5d — Regenerate-with-feedback + version chain UI
1. `src/feedback/regenerate.py`.
2. Replace stub in `app/lib/rating_runner.py`.
3. Extend Lead Detail with: regenerate button on thumbs-down + feedback, head/older-version expander, supersedes banner, "Show current" toggle.
4. Tests: `test_regenerate.py`.
5. Verification: thumbs-down a real email with comment → regenerate → new row visible → old row shows banner → toggle between current and older version works. Existing tests green.

### 5e — Dashboard integration
1. `list_unrated_content`, `rating_summary_per_content_type` queries.
2. Review queue + rating trends sections on Dashboard.
3. Verification: dashboard shows correct unrated count, click-through navigates to lead detail with the right lead pre-selected, trends chart renders or shows the "needs more data" hint.

---

## Open questions for you

1. **Re-rating behavior**: if an SDR clicks thumbs-up on already-up-rated content, do we (a) reject ("already rated"), (b) overwrite the prior rating, or (c) append a second `ContentRating` row? Spec is silent. **My recommendation: (a) reject + show toast "already rated"** — keeps rating-id idempotency simple in `process_ratings`. If you want re-rate-as-correction, (b) is the next-cleanest (we'd add an updated_at column).
2. **Rating window UX on older versions**: I have older versions read-only. Confirming you don't want SDRs rating historical versions — only the head?
3. **Where do the seed `manually_flagged: true` entries go in the migrated file**: spec says tag them `source: "engagement_reply"`. They're seeds, not real engagement-reply winners. I'll tag per spec — confirming this is fine (alternative: `source: "seed"`).

---

## Risks / unknowns

- `superseded_by_id` self-reference + `ratings` relationship require careful SQLAlchemy `remote_side` config so the ORM doesn't get confused. Will be mechanical — one line in the model.
- Negative example framing in prompts is critical: wrong framing teaches the model the wrong lesson. The text exactly: `"## Low-performing examples — DO NOT write like these. Avoid the patterns shown below."` will be locked into `format_negatives`, and `test_winners_loader.py` will assert this exact phrase appears.
- The migration of `winning_examples.json` runs in Phase 5b — if you've already done a manual edit between now and then, the script is idempotent and won't clobber additions; it only fills missing keys.
- `regenerate_with_feedback` calls a content generator, which makes a live LLM call (~2–4s). UI must show a spinner; backend must propagate exceptions cleanly so the UI's `st.error` path catches them.

---

## Sub-phase ordering (locked unless you redirect)

5a → review → 5b → review → 5c → review → 5d → review → 5e → review.
