"""Sync wrapper for async backend coroutines.

Streamlit reruns the script per interaction; we use a fresh event loop per call.

We do NOT call `nest_asyncio.apply()` at module import — it patches asyncio in
a way that breaks the Anthropic SDK's sniffio-based async-context detection
(e.g. `_base_client.request` → `asyncify(get_platform)` → `sniffio` raises
`AsyncLibraryNotFoundError`). Instead, apply nest_asyncio lazily only when
there is already a running loop (e.g. some Streamlit Cloud workers).
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — clean asyncio.run path. This is what Streamlit
        # gives us locally and on most Streamlit Cloud workers.
        return asyncio.run(coro)

    # Already inside a loop — patch in nest_asyncio so we can re-enter.
    import nest_asyncio
    nest_asyncio.apply(loop)
    return loop.run_until_complete(coro)
