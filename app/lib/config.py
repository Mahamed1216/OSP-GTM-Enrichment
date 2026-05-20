"""Secret resolution + Streamlit-Cloud bootstrap.

Two responsibilities:

1. ``get_secret(key)`` — read a secret with env-first, st.secrets-fallback
   semantics. Safe to call from any context (Streamlit script, subprocess,
   plain pytest), because the ``st.secrets`` access is guarded.

2. ``bootstrap_env_from_streamlit_secrets()`` — at app startup, copy every
   key from ``st.secrets`` into ``os.environ`` (without overwriting
   existing env vars). This is what makes Streamlit Cloud actually work:
   ``src.config.Settings`` is a ``pydantic-settings`` model and only
   reads from ``.env`` / ``os.environ``. Without this bridge, secrets set
   in the Streamlit Cloud dashboard would never reach ``Settings()``.

Call ``bootstrap_env_from_streamlit_secrets()`` BEFORE any ``from src.*``
import in ``app/main.py``, so the first ``Settings()`` instantiation sees
the merged environment. Subprocesses spawned later (``scripts/run_pipeline``,
``scripts/ingest_leads``) inherit the parent's env, so they also pick up
secrets that originated from ``st.secrets``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def _streamlit_secrets() -> Any | None:
    """Return the ``st.secrets`` mapping if available, else None.

    Importing streamlit inside the function (a) avoids an import-time
    cost when this module is loaded in a non-UI subprocess, and
    (b) tolerates environments where streamlit is uninstalled.
    """
    try:
        import streamlit as st  # local import on purpose
    except ImportError:
        return None
    try:
        return st.secrets
    except Exception:
        # No secrets.toml present and no Cloud injection — common in
        # local dev. Don't treat as an error.
        return None


def get_secret(key: str, default: Any = None) -> Any:
    """Read ``key`` from os.environ first, then st.secrets, else default.

    Env-first ordering means an explicit ``KEY=...`` in ``.env`` or the
    shell still wins over the deployed-cloud secret — useful for one-off
    local overrides.
    """
    val = os.environ.get(key)
    if val is not None and val != "":
        return val
    secrets = _streamlit_secrets()
    if secrets is None:
        return default
    try:
        out = secrets.get(key, default)
    except Exception:
        return default
    return out if out is not None and out != "" else default


def bootstrap_env_from_streamlit_secrets() -> int:
    """Mirror every key in ``st.secrets`` into ``os.environ``.

    Does NOT overwrite existing env vars — ``.env`` / shell exports win
    over Cloud secrets, matching ``get_secret``'s precedence.

    Returns the number of keys copied (0 in local dev with no secrets.toml).
    Idempotent: safe to call repeatedly.
    """
    secrets = _streamlit_secrets()
    if secrets is None:
        return 0
    copied = 0
    try:
        keys = list(secrets.keys())  # type: ignore[union-attr]
    except Exception:
        return 0
    for key in keys:
        if not isinstance(key, str):
            continue
        if key in os.environ and os.environ[key] != "":
            continue
        try:
            val = secrets[key]
        except Exception:
            continue
        # Only mirror scalar string-like values; nested tables (TOML sections)
        # don't fit env-var semantics and are skipped silently.
        if isinstance(val, (str, int, float, bool)):
            os.environ[key] = str(val)
            copied += 1
    if copied:
        log.info("bootstrap_env_from_streamlit_secrets", extra={"copied": copied})
    return copied
