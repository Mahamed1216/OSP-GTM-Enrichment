# OSP GTM Enrichment

Outbound GTM pipeline: **enrich → score → generate → deliver → learn**, with an
operator console on top.

A standalone app. Vercel hosts both halves, Supabase holds the data:

```
Next.js operator console  (pages/, components/)   ->  /
Python API                (api/index.py -> src/)  ->  /health, /api/*
Supabase Postgres         (supabase/schema.sql)   ->  16 tables
```

## What it does

1. **Ingest** leads from CSV, the external lead-source API, or
   `POST /api/v1/leads/process`. Leads are deduped per workspace and the raw
   source payload is preserved.
2. **Enrich** each lead — LinkedIn profile and company (Apify), company and
   industry news plus buyer research (Tavily).
3. **Score** against the workspace ICP config and assign a tier (A/B/C/D) with a
   rationale and the signals used.
4. **Generate** outbound content — email, and optionally call scripts and
   LinkedIn DMs (both off by default; they cost tokens).
5. **Deliver** through Instantly behind safety gates — tier floor, email
   verification, duplicate and placeholder checks. Nothing sends automatically
   from the API.
6. **Learn** — engagement sync, content ratings, and a prompt self-improvement
   loop gated on human approval.

Everything is workspace-scoped: one workspace per client or campaign, with its
own ICP config, prompts, winners and Instantly campaign.

## Run it locally

```bash
# 1. Install
pip install -r requirements.txt
npm install

# 2. Configure
cp .env.example .env    # then fill in DATABASE_URL and INTERNAL_API_KEY

# 3. Create the schema (once)
#    Either paste supabase/schema.sql into the Supabase SQL editor, or:
python scripts/init_db.py

# 4. Run both halves
uvicorn api.index:app --port 8000   # terminal 1 — the API
npm run dev                          # terminal 2 — the console, on :3000
```

`npm run dev` proxies `/health` and `/api/*` to the API on port 8000, so the
console behaves exactly as it does in production.

## Operator console

`/` is a statically prerendered Next.js page — no server-side rendering, no
environment variables, no database. Every panel fetches client-side and renders
its own error state, so the page loads even when the API is down.

| Tab | What it shows |
| --- | --- |
| Overview | API and database status, pipeline counts, leads by tier, recent runs, failed jobs, latest generated emails |
| Leads | Filterable table (search, tier, delivery state, enriched) with a per-lead detail drawer |
| Content | Generated emails / call scripts / LinkedIn DMs with safety and delivery state |
| Processing | Submit leads, drain the async queue, inspect run status and history |
| Settings | Which environment variables are configured (presence only), scoring thresholds, machine endpoints |

`/api/v1/*` requires the internal API key. It is **not** built into the bundle
and there is no unauthenticated server-side proxy — the operator pastes the key
into the console, where it stays in `sessionStorage` for that browser tab.

## API

| Route | Auth |
| --- | --- |
| `GET /health` | public |
| `GET /api/info` | public (booleans only) |
| `GET /api/v1/dashboard/summary` | bearer `INTERNAL_API_KEY` |
| `GET /api/v1/leads` | bearer |
| `GET /api/v1/leads/{id}` | bearer |
| `GET /api/v1/leads/{id}/processed` | bearer |
| `POST /api/v1/leads/process` | bearer |
| `GET /api/v1/runs` · `GET /api/v1/runs/{run_id}` | bearer |
| `POST /api/v1/drain` | bearer, or `CRON_SECRET` |
| `GET /api/v1/generated-content` | bearer |
| `GET /api/v1/settings/status` | bearer |
| `POST /api/instantly/reply-webhook` | `X-Webhook-Secret` |
| `POST /api/lead-source/run-scheduled` | `X-Job-Secret` |

The API never pushes to Instantly and never sends email — delivery is a separate,
explicitly triggered path.

## Deploying

- **Vercel** — framework preset **Next.js**, root `./`, all build overrides off.
  Full setup, routing and troubleshooting:
  [`docs/vercel_deployment.md`](docs/vercel_deployment.md).
- **Supabase** — paste [`supabase/schema.sql`](supabase/schema.sql) into the SQL
  editor. Project setup, the pooler connection string and verification:
  [`supabase/README.md`](supabase/README.md).

Check a deployment from the outside:

```bash
python scripts/check_deployment.py https://<your-app>.vercel.app
```

It verifies `/` serves the console, `/health` and `/api/info` serve JSON, and an
unknown path is handled by Next.js rather than falling through to the API.

## Repository layout

```
api/index.py        Vercel Python entrypoint; dispatches to the FastAPI apps
pages/              Next.js routes (operator console)
components/  lib/   console UI components and the API client
src/api/            internal API: server, processing, run store, worker
src/lib/            query + orchestration layer the API reads through
src/enrichment/     Apify + Tavily enrichment waterfall
src/content/        email, call script and LinkedIn DM generation
src/delivery/       Instantly push, eligibility gates, email verification
src/signals/        hiring-signal rescue and imported source signals
src/feedback/       engagement sync, ratings, prompt self-improvement
src/lead_source/    external lead-source client, ingest and scheduler
src/webhook/        Instantly reply webhook receiver
supabase/           schema.sql + setup guide
scripts/            CLI entry points and operational scripts
tests/              pytest suite
```

## Tests

```bash
pytest -q            # full Python suite
npm run build        # the console must build, and / must stay static
```
