# Production image for OSP GTM Enrichment.
#
# One image, many run modes (override the command):
#   A. Streamlit internal/admin UI : streamlit run app/main.py --server.port=8501 --server.address=0.0.0.0   (default CMD)
#   B. Internal API                : uvicorn src.api.server:app --host 0.0.0.0 --port 8000
#   C. Webhook service             : uvicorn src.webhook.server:app --host 0.0.0.0 --port 8001
#   D. Lead scheduler              : python -m src.lead_source.scheduler
#   E. SalesOS processing worker   : python -m src.integrations.salesos.worker --once --limit 10
#   F. SalesOS approved-send worker: python -m src.integrations.salesos.send_approved --once --limit 10
#   (API async worker              : python -m src.api.worker)
#
# SalesOS integration mode is the primary production model: the engine runs as a
# background worker (E/F) against the shared SalesOS Supabase DB. Streamlit (A)
# remains available as an internal admin/fallback UI. Set SALESOS_INTEGRATION_MODE=true.
#
# Secrets are NEVER baked in — all config comes from env vars at runtime
# (see .dockerignore, which excludes .env and .streamlit/secrets.toml).
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching. Install BOTH the core
# (requirements.txt: FastAPI/uvicorn/sqlalchemy/anthropic/...) and the UI
# (requirements-ui.txt: streamlit/pandas) so the same image can run any mode.
COPY requirements.txt requirements-ui.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-ui.txt

# Copy application source. Only the code dirs — local secrets/db/logs are
# excluded by .dockerignore so they can never leak into the image.
COPY src ./src
COPY app ./app
COPY data ./data
COPY README.md ./

# Run as a non-root user; give it a writable logs dir.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app
USER appuser

# Streamlit UI port by default; the API (8000) / webhook (8001) are exposed by
# overriding the command and publishing the matching port at run time.
EXPOSE 8501

# Default: launch the Streamlit UI (the current main UI entrypoint is app/main.py).
CMD ["streamlit", "run", "app/main.py", "--server.port=8501", "--server.address=0.0.0.0"]
