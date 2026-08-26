"""Run a coroutine from synchronous code.

Kept deliberately small: `asyncio.run` when there is no loop, and a lazily
applied `nest_asyncio` when a loop is already running (a scheduler entry point
calling into async pipeline code).

`nest_asyncio.apply()` is NOT called at import time on purpose — it patches
asyncio in a way that breaks the Anthropic SDK's sniffio-based async-context
detection (`_base_client.request` -> `asyncify(get_platform)` -> sniffio raises
`AsyncLibraryNotFoundError`).
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import nest_asyncio
    nest_asyncio.apply(loop)
    return loop.run_until_complete(coro)
