---
name: phase8-architecture
description: Phase 8 — External lead source API integration (pull-based, workspace-scoped, dedup, import log)
metadata:
  type: project
---

# Phase 8: External Lead Source API Integration

**Why:** Connect the external evergreen lead generation system (leads.osp.tools) to each workspace so sourced/enriched contacts flow into the local enrichment/scoring/content pipeline without manual CSV exports.

**How to apply:** Pull-based only (no auto-push, no auto-send). Each workspace configures its own client slug and API key. The import button is operator-initiated.

## API (leads.osp.tools)

- `GET /api/v1/health` — liveness check, no DB hit
- `GET /api/v1/clients/{slug}/contacts` — ContactList `{contacts:[...], count, limit, offset}`
  - Query params: `limit` (1–500), `offset`, `status`, `icp`, `include_suppressed`, `format`
  - Auth: `authorization` header (plain API key, no "Bearer" prefix required)
- Response schema: `ContactOut` — id (UUID), first_name, last_name, email, title, linkedin_url, company_name, company_domain, company_industry, signals[], source, enrichment_status, created_at

## New DB Objects

- `workspaces.lead_source_config` (JSON) — per-workspace settings (url, key, slug, limit, last fetch metadata)
- `leads.external_contact_id` (VARCHAR 256, indexed) — ContactOut UUID for primary dedup
- `leads.lead_source_raw` (JSON) — full ContactOut for provenance
- `lead_source_imports` table — one row per import run (fetched/created/updated/skipped/error counts, raw_summary JSON)

## New Source Modules

- `src/lead_source/settings.py` — `LeadSourceConfig` Pydantic model, `load/save_lead_source_config`, `mask_api_key`, `update_fetch_metadata`
- `src/lead_source/client.py` — `LeadSourceClient` (sync httpx), `health()`, `list_contacts()`, `test_connection()`
- `src/lead_source/mapper.py` — `map_contact(ContactOut→dict)`, `has_identity(fields)`
- `src/lead_source/ingest.py` — `ImportResult`, `import_contacts()`, `run_import()`, `start_import_log()`, `get_recent_imports()`

## UI Page

`app/pages/8_lead_sources.py` — Settings form, test connection button, fetch button (with limit), recent import history, pipeline handoff (session_state["lead_source_imported_ids"])

## Dedup Priority (workspace-scoped)

1. workspace_id + external_contact_id
2. workspace_id + email
3. workspace_id + linkedin_url
4. workspace_id + company_domain + first_name + last_name

On match: update empty fields only (field promotion, never overwrite).
On no match without email: skip as `no_email_no_match`.

## Skip Reasons

- `missing_identity` — no email, linkedin_url, or domain+name
- `no_email_no_match` — has linkedin/domain identity but no existing lead and no email to create
- `skipped_duplicate` — existing lead found but no new fields to update

## Security Constraints

- API key never logged — only `key_present: bool` logged
- UI shows only `mask_api_key()` output (last 4 chars)
- No auto-send, no auto-push to Instantly
- Imported leads start with no GeneratedContent, no delivery fields set

## Tests (15)

test_lead_source.py: settings isolation, cross-workspace slugs, connection success/failure mocking, import creates/scopes leads, cross-workspace email allowed, within-workspace email dedup, missing identity skip, raw payload stored, summary counts, API key masking, no auto-send, no Instantly push, pipeline ID handoff.
