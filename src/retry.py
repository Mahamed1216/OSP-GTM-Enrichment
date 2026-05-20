"""Shared retry decorator for external API calls.

Retries on transient network/server failures only. Never retries on 4xx
auth/validation errors — those won't get better by trying again.

Works for httpx, apify-client, and other SDKs that surface a status_code
attribute on the exception or its .response.
"""
import asyncio
import logging

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


def _status_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is None:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    return code


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, (ConnectionError, asyncio.TimeoutError, TimeoutError)):
        return True
    code = _status_of(exc)
    if code is not None:
        return 500 <= code < 600
    return False


retry_api = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception(_is_transient),
    reraise=True,
    before_sleep=before_sleep_log(log, logging.WARNING),
)
