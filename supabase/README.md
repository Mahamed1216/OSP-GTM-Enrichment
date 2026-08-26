# Supabase setup

The database schema lives in [`schema.sql`](schema.sql), generated from
`src/models.py` by `scripts/gen_supabase_schema.py`. `src/models.py` is the only
source of truth in this repo — there is no Alembic, no `migrations/` directory
and no other checked-in SQL.

## 1. Create the project

1. <https://supabase.com/dashboard> → **New project**.
2. Pick a region close to your Vercel region and set a strong database password.
   **Save that password** — it is the only time Supabase shows it, and it is part
   of the connection string.
3. Wait for provisioning to finish.

## 2. Run the schema

**SQL Editor** → **New query** → paste the entire contents of
[`schema.sql`](schema.sql) → **Run**.

It creates 16 tables, 41 indexes, seeds the default `osp` workspace, and enables
RLS. Every statement is `IF NOT EXISTS` / idempotent, so re-running it is safe.

No extensions are needed — every primary key is a `SERIAL` integer and nothing
uses `uuid` or `pgcrypto`.

## 3. Get the DATABASE_URL

**Project Settings → Database → Connection string → URI.**

Use the **Session pooler / Transaction pooler** URI (port `6543`), not the direct
connection on port `5432`. Serverless functions open and drop connections
constantly and will exhaust a direct Postgres connection limit.

It looks like:

```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
```

Replace `<password>` with the database password from step 1, URL-encoding any
special characters (`@` → `%40`, `#` → `%23`, and so on).

If Supabase hands you a `postgres://…` URI, it still works: `src/db.py`
rewrites that prefix to `postgresql://`, because `postgres` is not a SQLAlchemy
2.0 dialect name and would otherwise raise at import time.

## 4. Vercel environment variables

**Project → Settings → Environment Variables** (Production + Preview):

| Variable | Required | Why |
| --- | --- | --- |
| `DATABASE_URL` | **yes** | the pooler URI from step 3 |
| `INTERNAL_API_KEY` | **yes** | bearer auth for `/api/v1/*`; any long random string |
| `ANTHROPIC_API_KEY` | for processing | scoring + content generation |
| `TAVILY_API_KEY` | for processing | buyer research / hiring signals |
| `APIFY_API_TOKEN` | for processing | LinkedIn enrichment |
| `INSTANTLY_WEBHOOK_SECRET` | for the reply webhook | validates `X-Webhook-Secret` |
| `LEAD_SOURCE_JOB_SECRET` | for the scheduler | validates `X-Job-Secret` |
| `INSTANTLY_API_KEY` | optional | only if the pipeline reads Instantly |
| `CRON_SECRET` | optional | lets a Vercel Cron call `/api/v1/drain` |

Redeploy after adding them — environment variables are read at runtime, but a
running deployment does not pick up new ones until it is redeployed.

Do **not** put the Supabase anon or service-role key here. The app talks to
Postgres directly over SQLAlchemy; it never uses the Supabase client SDKs.

## 5. Test the connection

Locally, against the same database:

```bash
DATABASE_URL='postgresql://postgres.<ref>:<password>@…pooler.supabase.com:6543/postgres' \
  python -c "from src.db import engine; from sqlalchemy import text; \
  print(engine.connect().execute(text('select current_database(), version()')).one())"
```

On Windows PowerShell:

```powershell
$env:DATABASE_URL='postgresql://…'
py -c "from src.db import engine; from sqlalchemy import text; print(engine.connect().execute(text('select current_database()')).one())"
```

On the deployment, `GET /health` reports it without any key:

```json
{ "status": "ok", "backend_importable": true, "database_configured": true }
```

`"status": "degraded"` means the response also carries a `backend_error` or
`database_error` naming the problem.

## 6. Verify the tables exist

In the Supabase **Table Editor** you should see 16 tables. Or in the SQL editor:

```sql
select table_name from information_schema.tables
where table_schema = 'public' order by table_name;

select id, name, slug, is_default from workspaces;  -- expect one row: OSP / osp
```

## 7. If Vercel says `DATABASE_URL not configured`

That error is deliberate — the function refuses database-backed routes rather
than failing deep inside a query. Work through:

1. Is `DATABASE_URL` set for the **Production** environment (not only Preview)?
2. Did you **redeploy** after adding it?
3. Is the value non-empty? An empty variable reads as unset.
4. Check `GET /api/info` — `database_configured` tells you whether the function
   can see the variable at all.

Other failures `/health` will name for you:

| `backend_error` contains | Meaning |
| --- | --- |
| `ModuleNotFoundError: No module named 'src'` | the function bundle is missing the pipeline code — check `includeFiles` in `vercel.json` |
| `Can't load plugin: sqlalchemy.dialects:postgres` | a `postgres://` URL reached an older code path; make sure this commit is deployed |
| `password authentication failed` | wrong password, or special characters not URL-encoded |
| `Connection refused` / timeout | using the direct `5432` URI instead of the pooler |

## Can the app create the tables itself?

Yes — `init_db()` in `src/db.py` runs `Base.metadata.create_all(engine)` and
seeds the default workspace. But **the Vercel function deliberately never calls
it**: schema DDL on every cold start is slow and racy. So either paste
`schema.sql` (recommended), or run it once from your machine:

```bash
DATABASE_URL='postgresql://…' python scripts/init_db.py
```

Both paths produce the same schema — verified by creating the tables from
`schema.sql` and diffing them against `Base.metadata`: 16/16 tables, zero column
drift.

## What is in the schema

Required for the app to boot:

| Table | Purpose |
| --- | --- |
| `workspaces` | operating context, one per client/campaign; everything else is scoped by `workspace_id` |
| `leads` | the contacts the pipeline works on; unique on `(email, workspace_id)` |

Core pipeline (needed for lead processing):

| Table | Purpose |
| --- | --- |
| `enrichments` | LinkedIn / company / buyer-research result per lead |
| `scores` | ICP fit score and tier per lead |
| `generated_contents` | one row per email, call script or LinkedIn DM |
| `api_runs` | tracking for `POST /api/v1/leads/process` |

Optional features — create them anyway (they cost nothing), but nothing breaks
if a feature goes unused:

| Table | Feature |
| --- | --- |
| `engagements`, `instantly_analytics_snapshots` | Instantly delivery + analytics sync |
| `reply_threads`, `reply_drafts` | Reply Agent / reply webhook |
| `content_ratings`, `prompt_recommendations`, `prompt_configs`, `winning_examples` | self-improvement loop and prompt editing |
| `lead_signals` | hiring-signal rescue and imported source signals |
| `lead_source_imports` | external lead-source imports |

### Status values the app writes

These are plain `VARCHAR` columns, not Postgres enums, so no type needs
creating:

| Column | Values |
| --- | --- |
| `scores.tier` | `A`, `B`, `C`, `D` |
| `api_runs.status` | `queued`, `running`, `completed`, `failed`, `partial` |
| `api_runs.run_mode` | `sync`, `async` |
| `lead_signals.status` | `not_started`, `completed`, `skipped`, `failed` |
| `lead_signals.tier_uplift_recommendation` | `none`, `C_to_B`, `C_to_A`, `B_to_A` |
| `generated_contents.kind` | `email`, `call_script`, `linkedin_msg` |
| `generated_contents.delivery_status` | `sent`, `error`, `in_progress`, `NULL` |
| `prompt_configs.channel` | `email` (`NULL` for non-prompt actions) |

### A second schema exists — you do not need it

`src/integrations/salesos/models.py` defines 7 more `salesos_*` tables. They are
only created when `SALESOS_INTEGRATION_MODE=true`, which is off by default and
out of scope for this deployment. `schema.sql` deliberately excludes them.
