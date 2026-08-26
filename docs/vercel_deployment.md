# Deploying the API + webhook on Vercel

Vercel hosts the **FastAPI surface** of this repo as one Python serverless
function. It does **not** host the Streamlit operator UI (`app/`) — Streamlit
needs a long-lived, stateful websocket process, which serverless cannot
provide. Keep the UI on Streamlit Cloud (or any container host) pointed at the
same `DATABASE_URL`.

## What gets deployed

| Route | Backed by | Auth |
| --- | --- | --- |
| `GET /health` | `src/api/server.py` | public |
| `POST /api/v1/leads/process` | `src/api/server.py` | `Authorization: Bearer $INTERNAL_API_KEY` |
| `GET /api/v1/runs/{run_id}` | `src/api/server.py` | `Authorization: Bearer $INTERNAL_API_KEY` |
| `GET /api/v1/leads/{id}/processed` | `src/api/server.py` | `Authorization: Bearer $INTERNAL_API_KEY` |
| `POST /api/v1/drain` | `api/index.py` → `src/api/worker.py` | Bearer `$INTERNAL_API_KEY` or `$CRON_SECRET` |
| `POST /api/instantly/reply-webhook` | `src/webhook/server.py` | `X-Webhook-Secret` |
| `POST /api/lead-source/run-scheduled` | `src/webhook/server.py` | `X-Job-Secret` |

`api/index.py` is the only function. It puts the repo root on `sys.path` and
dispatches by path prefix to the two existing FastAPI apps, so their routes,
middleware (webhook body-size + rate-limit guards) and auth are unchanged.

## Vercel project settings

| Setting | Value |
| --- | --- |
| Framework preset | **Other** |
| Root directory | `./` (repo root) |
| Build command | *(empty — override off)* |
| Output directory | `public` (already set in `vercel.json`) |
| Install command | *(empty — Vercel installs `requirements.txt` automatically)* |
| Node.js version | irrelevant (no JS build) |
| Python version | 3.12 (Vercel default; repo requires >= 3.11) |

## Required environment variables

Set these in **Project → Settings → Environment Variables** (Production +
Preview). `.env` is never uploaded (`.vercelignore`).

| Variable | Why |
| --- | --- |
| `DATABASE_URL` | **Required.** Postgres. The default `sqlite:///sdr.db` cannot work — the serverless filesystem is read-only. Use a pooled connection string (pgBouncer / Supabase pooler / Neon pooler). |
| `ANTHROPIC_API_KEY` | scoring + content generation |
| `INTERNAL_API_KEY` | bearer auth for `/api/v1/*` |
| `INSTANTLY_WEBHOOK_SECRET` | Instantly reply webhook |
| `LEAD_SOURCE_JOB_SECRET` | lead-source scheduler endpoint |
| `TAVILY_API_KEY` | buyer research / hiring signals |
| `APIFY_API_TOKEN` | LinkedIn enrichment |
| `INSTANTLY_API_KEY` | only if the pipeline needs Instantly reads |
| `CRON_SECRET` | optional; lets a Vercel Cron call `/api/v1/drain` |

The schema is **not** created by the function on every cold start. Initialize
it once from anywhere with the same `DATABASE_URL`: `python scripts/init_db.py`.

## Serverless caveats

* **Function duration.** `vercel.json` sets `maxDuration: 60` (the Hobby cap).
  A `run_mode: "sync"` request that enriches + scores + generates for a batch
  will exceed that. Send small batches, or use `run_mode: "async"` and drain.
* **Async runs need a drainer.** `python -m src.api.worker` has no long-lived
  process here. Call `POST /api/v1/drain?batch=3` externally, or add a Vercel
  Cron (set `CRON_SECRET` first):

  ```json
  "crons": [{ "path": "/api/v1/drain?batch=3", "schedule": "*/15 * * * *" }]
  ```

* **DB connections.** `src/db.py` switches to `NullPool` + `pool_pre_ping` when
  the `VERCEL` env var is present, so short-lived instances don't leak
  connections. A pooled `DATABASE_URL` is still recommended.
* **Rate limiting.** The webhook's in-process limiter is per instance, so the
  effective limit is looser than on a single container.

## Verify locally

```bash
pip install -r requirements.txt
uvicorn api.index:app --port 8000
curl http://localhost:8000/health
```
