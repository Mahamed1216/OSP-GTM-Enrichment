"""Launch the Instantly reply webhook receiver.

Usage
-----
Local (alongside Streamlit on port 8501):
    python run_webhook.py

Production (uvicorn directly):
    uvicorn src.webhook.server:app --host 0.0.0.0 --port 8001 --workers 1

Environment variables required:
    DATABASE_URL              — same value as the Streamlit app
    INSTANTLY_WEBHOOK_SECRET  — secret you configure in the Instantly HTTP Request action
    ANTHROPIC_API_KEY         — for LLM classification

Optional:
    WEBHOOK_PORT              — default 8001
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

from src.db import init_db
init_db()

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", "8001"))
    print(f"Starting OSP reply webhook receiver on port {port}…")
    uvicorn.run("src.webhook.server:app", host="0.0.0.0", port=port, reload=False)
