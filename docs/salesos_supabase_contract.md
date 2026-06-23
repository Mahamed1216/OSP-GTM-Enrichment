# SalesOS ↔ Engine — Shared Supabase Data Contract

This document defines the **data contract** between the **SalesOS app** (the
primary CSM-facing UI) and the **OSP GTM Enrichment engine** (a Dockerized
background worker). It is the integration spec for `SALESOS_INTEGRATION_MODE=true`.

> **Status: proposed / adapter-backed.** SalesOS owns the canonical Supabase
> schema and it is **not finalized**. The engine does **not** hard-code that
> schema. Instead it ships a small set of **contract tables** (the shapes below,
> created with a `salesos_` prefix) plus a single **adapter layer**
> (`src/integrations/salesos/`). When SalesOS's real table/column names are
> confirmed, the integration is reconciled in exactly one of two ways without
> touching the engine's business logic:
>
> 1. **Database views** — create Postgres views named `salesos_*` over SalesOS's
>    real tables (recommended; zero engine code change), **or**
> 2. **Adapter remap** — change the table/column mapping in
>    `src/integrations/salesos/models.py` + `adapter.py` only.
>
> Everything the engine reads/writes goes through the adapter, so the rest of
> the pipeline (enrichment, scoring, research, content, delivery) never needs to
> know SalesOS's physical schema.

---

## Architecture (integration mode)

```
SalesOS Leads tab  ──sources lead──▶  shared Supabase: leads
                                          │
                            ┌─────────────┘  (a queued outbound_job is created)
                            ▼
                 Engine processing worker  (python -m src.integrations.salesos.worker)
                   • claims queued job
                   • imports SalesOS lead → engine's internal Lead (reuses dedup,
                     raw-payload storage, source-signal normalization, source-tier
                     separation, email_verified mapping)
                   • runs enrichment + buyer research + signals + scoring + content
                   • writes results back to: lead_enrichments, lead_scores,
                     outbound_content  (content_status = pending_review)
                            │
                            ▼
SalesOS Outbound tab  ──CSM reviews / edits / approves──▶  outbound_approvals
                            │                                (approval_status = approved)
                            ▼
                 Engine send worker  (python -m src.integrations.salesos.send_approved)
                   • finds approved-but-unsent content
                   • re-runs ALL safety checks (content/tier/verify/dedupe)
                   • requires approval_status = approved  ← tier alone is NOT enough
                   • sends through Instantly
                   • writes delivery_events  (+ engagement/reply sync later)
```

**Hard rules in integration mode**

- No lead is pushed/sent to Instantly unless **all** of: content exists,
  content safety passes, email is verified, no duplicate send exists, tier meets
  the configured threshold, **and** `outbound_approvals.approval_status =
  approved`. Missing approval blocks with reason `missing_salesos_csm_approval`
  and the lead is **never** marked sent.
- The engine never auto-sends on tier alone.
- The engine never overwrites local tiering with the source tier — `source_tier`
  is stored separately from the engine's computed `tier`.

---

## Tables

All ids below are opaque strings (Supabase `uuid`/`text`). `workspace_id` /
`client_id` carry the SalesOS tenant boundary; the engine maps the SalesOS
tenant to its internal workspace for isolation. Timestamps are UTC.

### 1. `leads` (contract table: `salesos_leads`)
Leads sourced by a CSM in the SalesOS Leads tab (often via Dele's sourcing).

| field                 | type      | notes                                                  |
| --------------------- | --------- | ------------------------------------------------------ |
| `id`                  | string PK | SalesOS lead id                                        |
| `workspace_id`        | int       | engine workspace mapping (tenant isolation)            |
| `client_id`           | string    | SalesOS-native tenant id (alternative to workspace_id) |
| `external_contact_id` | string    | sourcing-system contact id (Dele UUID); dedup anchor   |
| `source`              | string    | e.g. `salesos`, `osp_lead_engine`                      |
| `first_name`          | string    |                                                        |
| `last_name`           | string    |                                                        |
| `title`               | string    |                                                        |
| `email`               | string    |                                                        |
| `email_verified`      | bool      | mapped into the engine's verification columns          |
| `linkedin_url`        | string    |                                                        |
| `company_name`        | string    |                                                        |
| `company_domain`      | string    |                                                        |
| `company_website`     | string    |                                                        |
| `company_industry`    | string    |                                                        |
| `raw_source_payload`  | json      | full source payload, stored verbatim for provenance    |
| `source_signals`      | json      | sourcing signals (preserved, never discarded)          |
| `source_tier`         | string    | sourcing tier — stored SEPARATELY from engine tier     |
| `source_tier_score`   | float     | sourcing tier score                                    |
| `created_at`          | timestamp |                                                        |
| `updated_at`          | timestamp |                                                        |

### 2. `outbound_jobs` (contract table: `salesos_outbound_jobs`)
A unit of work: "process this lead for outbound." Created when a CSM marks a
lead for outbound (or by an automation). Also referred to as
`outbound_processing_runs`.

| field          | type      | notes                                                   |
| -------------- | --------- | ------------------------------------------------------- |
| `id`           | string PK |                                                         |
| `lead_id`      | string FK | → `salesos_leads.id`                                    |
| `workspace_id` | int       |                                                         |
| `client_id`    | string    |                                                         |
| `status`       | string    | `queued` / `running` / `completed` / `failed` / `skipped` |
| `requested_by` | string    | CSM / system identity                                   |
| `requested_at` | timestamp |                                                         |
| `started_at`   | timestamp |                                                         |
| `completed_at` | timestamp |                                                         |
| `error`        | text      | failure detail when `status=failed`                     |
| `options`      | json      | per-job toggles (see **Content options** below)         |
| `engine_lead_id` | int     | engine-side `leads.id` once imported (link)             |

### 3. `lead_enrichments` (contract table: `salesos_lead_enrichments`)
Enrichment output written back per lead.

| field                   | type      | notes                                |
| ----------------------- | --------- | ------------------------------------ |
| `id`                    | string PK |                                      |
| `lead_id`               | string FK | → `salesos_leads.id`                 |
| `linkedin_profile`      | json      |                                      |
| `company_details`       | json      |                                      |
| `company_news`          | json      |                                      |
| `industry_news`         | json      |                                      |
| `buyer_account_research`| json      | buyer-account discovery result       |
| `tavily_metadata`       | json      | research window / provider metadata  |
| `source_status`         | json      | per-source success/error/duration    |
| `created_at`            | timestamp |                                      |
| `updated_at`            | timestamp |                                      |

### 4. `lead_scores` (contract table: `salesos_lead_scores`)

| field           | type      | notes                                   |
| --------------- | --------- | --------------------------------------- |
| `id`            | string PK |                                         |
| `lead_id`       | string FK | → `salesos_leads.id`                    |
| `score`         | int       |                                         |
| `tier`          | string    | engine-computed tier (NOT source_tier)  |
| `rationale`     | text      |                                         |
| `signals_used`  | json      |                                         |
| `model_version` | string    | scoring model id                        |
| `scored_at`     | timestamp |                                         |

### 5. `outbound_content` (contract table: `salesos_outbound_content`)
Generated outreach awaiting CSM review in the Outbound tab.

| field            | type      | notes                                                                   |
| ---------------- | --------- | ----------------------------------------------------------------------- |
| `id`             | string PK |                                                                         |
| `lead_id`        | string FK | → `salesos_leads.id`                                                     |
| `email_subject`  | string    |                                                                         |
| `email_body`     | text      |                                                                         |
| `call_script`    | text      | nullable — only when call-script generation is enabled                  |
| `linkedin_message`| text     | nullable — only when LinkedIn generation is enabled                     |
| `content_status` | string    | `generated` / `pending_review` / `edited` / `approved` / `rejected` / `sent` |
| `safety_status`  | string    | `ok` / `needs_review`                                                    |
| `blocked_reason` | string    | safety/eligibility block code when not sendable                         |
| `prompt_version` | string    |                                                                         |
| `model_version`  | string    |                                                                         |
| `engine_content_id` | int    | engine-side `generated_contents.id` for the email (link)                |
| `created_at`     | timestamp |                                                                         |
| `updated_at`     | timestamp |                                                                         |

> **Content options.** Call script + LinkedIn message are **off by default** for
> cost control (same as standalone). SalesOS chooses what to generate per job
> via `outbound_jobs.options`:
> `{"generate_email": true, "generate_call_script": false, "generate_linkedin": false}`.
> Email-only is the default.

### 6. `outbound_approvals` (contract table: `salesos_outbound_approvals`)
The CSM's review decision. **Source of truth for "may send."**

| field                   | type      | notes                              |
| ----------------------- | --------- | ---------------------------------- |
| `id`                    | string PK |                                    |
| `lead_id`               | string FK | → `salesos_leads.id`               |
| `content_id`            | string FK | → `salesos_outbound_content.id`    |
| `approved_by`           | string    | CSM identity                       |
| `approved_at`           | timestamp |                                    |
| `approval_status`       | string    | `pending` / `approved` / `rejected`|
| `edited_subject`        | string    | CSM edit (overrides generated)     |
| `edited_body`           | text      | CSM edit (overrides generated)     |
| `edited_call_script`    | text      | CSM edit                           |
| `edited_linkedin_message`| text     | CSM edit                           |
| `notes`                 | text      |                                    |

### 7. `delivery_events` (contract table: `salesos_delivery_events`)
Send + engagement outcomes written back after delivery / sync.

| field              | type      | notes                                          |
| ------------------ | --------- | ---------------------------------------------- |
| `id`               | string PK |                                                |
| `lead_id`          | string FK | → `salesos_leads.id`                           |
| `content_id`       | string FK | → `salesos_outbound_content.id`                |
| `destination`      | string    | e.g. `instantly`                               |
| `status`           | string    | `sent` / `blocked` / `failed` / `skipped`      |
| `sent_at`          | timestamp |                                                |
| `instantly_lead_id`| string    | remote id returned by Instantly                |
| `campaign_id`      | string    |                                                |
| `error`            | text      | block/failure detail                           |
| `engagement_status`| string    | opened / clicked / etc. (from engagement sync) |
| `reply_status`     | string    | replied / sentiment (from reply sync)          |

---

## Mapping notes for the SalesOS team

- The engine maps SalesOS `leads.source_signals` → its normalized signal rows and
  promotes `source_tier`/`source_tier_score` **separately** from the engine's own
  computed score, so a strong source tier never silently overrides the engine's
  judgment.
- The engine reuses its existing import/dedup path, so `external_contact_id` +
  `source` is the most stable dedup key; `email`, `linkedin_url`, and
  `company_domain + name` are fallbacks.
- The engine writes `content_status = pending_review` on generated content; it
  never advances content to `sent` without an `approved` `outbound_approvals`
  row **and** a clean re-run of all safety gates.

## Still needed from the SalesOS team

1. Confirmed physical table/column names (so we can choose **views** vs **adapter
   remap**).
2. The SalesOS tenant identifier (`client_id` vs `workspace_id`) and the agreed
   engine-workspace mapping.
3. Where/how `outbound_jobs` rows are created (CSM action, trigger, or queue) and
   who sets `requested_by`.
4. Auth/connection details for the shared Supabase (engine connects via
   `DATABASE_URL`).
5. Whether SalesOS wants engagement/reply sync written to `delivery_events` or a
   separate SalesOS-owned table.
