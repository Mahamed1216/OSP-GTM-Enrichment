"""Secret lookup for the backend.

Environment variables only. This used to fall back to ``st.secrets`` because
the operator UI ran on a different host; the app now runs on Vercel, where
configuration is plain environment variables, so the fallback is gone.
"""
from __future__ import annotations

import os
from typing import Any


def get_secret(key: str, default: Any = None) -> Any:
    """Return ``key`` from the environment, or ``default`` if unset/empty."""
    value = os.environ.get(key)
    if value is None or value == "":
        return default
    return value
