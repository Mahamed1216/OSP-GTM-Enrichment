# Deploying on Vercel

Vercel hosts two things from this repo:

1. **The operator console** — a small Next.js app (`pages/`) served at `/`.
2. **The API** — the existing FastAPI code, as one Python serverless function
   (`api/index.py`) serving `/health` and `/api/*`.

It does **not** host the Streamlit operator UI (`app/`). Streamlit needs a
long-lived, stateful websocket process, which serverless cannot provide. Keep it
on Streamlit Cloud (or any container host) pointed at the same `DATABASE_URL`.

## Routing

`next.config.js` owns the routing. Next.js serves the UI; two rewrites hand the
API paths to the Python function:

| Path | Served by |
| --- | --- |
| `/` and any other UI route | Next.js (`pages/index.jsx`) |
| `/health` | `api/index.py` |
| `/api/*` | `api/index.py` |

In development those two rewrites point at `http://127.0.0.1:8000` instead, so a
local `uvicorn` process backs the dev server. Override the origin with
`PY_API_ORIGIN` if you run it on another port.

## API surface (unchanged)

| Route | Backed by | Auth |
| --- | --- | --- |
| `GET /health` | `api/index.py` | public |
| `GET /api/info` | `api/index.py` | public (booleans only) |
| `POST /api/v1/leads/process` | `src/api/server.py` | `Authorization: Bearer $INTERNAL_API_KEY` |
| `GET /api/v1/runs/{run_id}` | `src/api/server.py` | bearer `$INTERNAL_API_KEY` |
| `GET /api/v1/leads/{id}/processed` | `src/api/server.py` | bearer `$INTERNAL_API_KEY` |
| `POST /api/v1/drain` | `api/index.py` → `src/api/worker.py` | bearer `$INTERNAL_API_KEY` or `$CRON_SECRET` |
| `POST /api/instantly/reply-webhook` | `src/webhook/server.py` | `X-Webhook-Secret` |
| `POST /api/lead-source/run-scheduled` | `src/webhook/server.py` | `X-Job-Secret` |

`api/index.py` puts the repo root on `sys.path` and dispatches by path prefix to
the two existing FastAPI apps, so their routes, middleware (webhook body-size +
rate-limit guards) and auth are unchanged.

### Nothing crashes out of the function

A crash out of a Vercel function is an opaque `FUNCTION_INVOCATION_FAILED` page
with no way to tell what went wrong from the outside. So `api/index.py` imports
nothing heavier than the standard library at module scope, loads the backend
lazily, and converts every failure into JSON that names the cause:

| Situation | Response |
| --- | --- |
| Pipeline code missing from the bundle | `503 {"error": "ModuleNotFoundError: No module named 'src'", "hint": …}` |
| `DATABASE_URL` unset on Vercel | `503 {"error": "DATABASE_URL not configured"}` |
| Anything unhandled in a FastAPI app | `500 {"error": "<type>: <message>"}` |

`GET /health` answers in every one of those cases — 200 when healthy, 503 with
`backend_error` / `database_error` when not. It is the first thing to check on a
misbehaving deployment.

`vercel.json`'s `includeFiles` is what puts `src/`, `app/` and `data/` in the
function bundle. Vercel traces imports statically and cannot see the runtime
`sys.path` insert, so without it the function would deploy and then fail with
`ModuleNotFoundError: No module named 'src'`.

## How the console authenticates

`/api/v1/*` requires the internal API key. The console does **not** ship the key
in its bundle, and there is no unauthenticated server-side proxy that would
expose lead processing to the internet. The operator pastes the key into the
Access card; it is held in `sessionStorage` (that browser tab only) and sent
straight to the same-origin API.

That means the Vercel URL itself is public but can do nothing privileged without
the key. If you want the console behind a login as well, use Vercel's
[Deployment Protection](https://vercel.com/docs/deployment-protection).

## Vercel project settings

| Setting | Value |
| --- | --- |
| Framework preset | **Next.js** (auto-detected from `package.json`) |
| Root directory | `./` (repo root) |
| Build command | *default* — `next build` |
| Output directory | *default* — `.next` |
| Install command | *default* — `npm install` |
| Node.js version | 22.x (Vercel default) |
| Python version | 3.12 (Vercel default; repo requires >= 3.11) |

Leave all four build overrides **off**. Vercel builds the Next.js app from
`package.json` and, separately, builds `api/index.py` into a Python function
using the root `requirements.txt`. `vercel.json` only configures that function —
its `maxDuration` and `includeFiles`.

## Required environment variables

Set these in **Project → Settings → Environment Variables** (Production +
Preview). `.env` is never uploaded (`.vercelignore`).

| Variable | Why |
| --- | --- |
| `DATABASE_URL` | **Required.** Postgres. The default `sqlite:///sdr.db` cannot work — the serverless filesystem is read-only. Use a pooled connection string (Supabase pooler on port 6543, pgBouncer, Neon pooler). Setting up a fresh database: [`supabase/README.md`](../supabase/README.md). |
| `ANTHROPIC_API_KEY` | scoring + content generation |
| `INTERNAL_API_KEY` | bearer auth for `/api/v1/*` |
| `INSTANTLY_WEBHOOK_SECRET` | Instantly reply webhook |
| `LEAD_SOURCE_JOB_SECRET` | lead-source scheduler endpoint |
| `TAVILY_API_KEY` | buyer research / hiring signals |
| `APIFY_API_TOKEN` | LinkedIn enrichment |
| `INSTANTLY_API_KEY` | only if the pipeline needs Instantly reads |
| `CRON_SECRET` | optional; lets a Vercel Cron call `/api/v1/drain` |

Nothing here is exposed to the browser — no variable is prefixed
`NEXT_PUBLIC_`, so none is inlined into the client bundle.

The schema is **not** created by the function on every cold start. Create it once
by pasting [`supabase/schema.sql`](../supabase/schema.sql) into the Supabase SQL
editor, or by running `python scripts/init_db.py` against the same
`DATABASE_URL`.

## Serverless caveats

* **Function duration.** `vercel.json` sets `maxDuration: 60` (the Hobby cap). A
  `run_mode: "sync"` request that enriches + scores + generates for a batch will
  exceed that. Send small batches, or use `run_mode: "async"` and drain.
* **Async runs need a drainer.** `python -m src.api.worker` has no long-lived
  process here. Use the console's **Drain queued** button, call
  `POST /api/v1/drain?batch=3` externally, or add a Vercel Cron (set
  `CRON_SECRET` first):

  ```json
  "crons": [{ "path": "/api/v1/drain?batch=3", "schedule": "*/15 * * * *" }]
  ```

* **DB connections.** `src/db.py` switches to `NullPool` + `pool_pre_ping` when
  the `VERCEL` env var is present, so short-lived instances don't leak
  connections. A pooled `DATABASE_URL` is still recommended.
* **Rate limiting.** The webhook's in-process limiter is per instance, so the
  effective limit is looser than on a single container.

## Verify locally

The UI build on its own:

```bash
npm install
npm run build
```

The whole thing, UI + API, the way it behaves in production:

```bash
# terminal 1 — the API
pip install -r requirements.txt
uvicorn api.index:app --port 8000

# terminal 2 — the UI (proxies /health and /api/* to :8000)
npm run dev
```

Then open <http://localhost:3000>. `curl localhost:3000/health` should return
the API's JSON, not an HTML page.
