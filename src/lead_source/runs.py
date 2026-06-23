"""Trigger + poll a sourcing run in the external lead source system.

Dele's sourcing system is signal-first: SalesOS / this system can trigger a
sourcing run, poll it to completion, then pull the resulting contacts. This is
the OPTIONAL pre-import step gated by the per-workspace
``trigger_run_before_import`` setting. When that flag is false (the default),
this module is never invoked and the normal contacts-polling path runs unchanged.

Flow (only when explicitly enabled):
  1. POST /api/v1/clients/{slug}/runs        → start a run, capture run_id
  2. GET  /api/v1/runs/{run_id}  (repeat)    → poll until terminal or timeout
  3. on "completed" → caller pulls contacts and imports normally
     on failed/timeout/error → caller does NOT import (no silent import)

Safeguards: max wait time, poll interval, clear failed/timeout status, run_id,
run status, and an error string are all captured on the returned RunResult and
persisted by the scheduler.

This module is deliberately decoupled from the network/clock so it is fully
unit-testable: ``sleep_fn`` and ``monotonic_fn`` are injectable, and the only
I/O is through the passed-in client's ``trigger_run`` / ``get_run`` methods.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

# Terminal status vocabularies. We classify generously because the exact
# strings Dele's API returns are not contractually fixed.
_SUCCESS = {"completed", "complete", "succeeded", "success", "done", "finished", "ok"}
_FAILURE = {
    "failed", "fail", "error", "errored", "cancelled", "canceled",
    "aborted", "timeout", "timed_out", "rejected",
}

# Returned statuses (NOT raw API statuses): the scheduler stores these so an
# operator can tell completed/failed/timeout/error apart at a glance.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"


def classify_run_status(status: str | None) -> str:
    """Map a raw run status to one of: running | completed | failed."""
    s = (status or "").strip().lower()
    if s in _SUCCESS:
        return "completed"
    if s in _FAILURE:
        return "failed"
    return "running"


def _extract_run_id(run: dict[str, Any] | None) -> str | None:
    if not isinstance(run, dict):
        return None
    for key in ("run_id", "id", "runId", "uuid"):
        val = run.get(key)
        if val not in (None, ""):
            return str(val)
    return None


def _extract_status(run: dict[str, Any] | None) -> str | None:
    if not isinstance(run, dict):
        return None
    for key in ("status", "state", "run_status", "phase"):
        val = run.get(key)
        if val not in (None, ""):
            return str(val)
    return None


def _extract_error(run: dict[str, Any] | None) -> str | None:
    if not isinstance(run, dict):
        return None
    for key in ("error", "error_message", "message", "detail", "reason"):
        val = run.get(key)
        if val not in (None, ""):
            return str(val)[:2000]
    return None


@dataclass
class RunResult:
    """Outcome of a trigger+poll cycle.

    ``completed`` is True ONLY when the run reached a success status. The
    scheduler imports contacts iff ``completed`` is True; every other outcome
    (failed / timeout / error) blocks the import and is surfaced clearly.
    """
    triggered: bool
    run_id: str | None
    status: str          # completed | failed | timeout | error
    completed: bool
    error: str | None = None
    polls: int = 0


def trigger_and_poll_run(
    client: Any,
    client_slug: str,
    *,
    poll_interval_seconds: int = 10,
    max_wait_seconds: int = 600,
    sleep_fn: Callable[[float], Any] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Trigger a sourcing run and poll it until terminal or timeout.

    Returns a RunResult. Never raises — trigger/poll exceptions are captured so
    the caller can record a failed run and skip the import rather than crash.
    """
    poll_interval_seconds = max(1, int(poll_interval_seconds))
    max_wait_seconds = max(0, int(max_wait_seconds))

    # 1. Trigger the run.
    try:
        run = client.trigger_run(client_slug)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        log.warning("lead_source_run_trigger_failed", extra={"slug": client_slug, "error": err})
        return RunResult(triggered=True, run_id=None, status=STATUS_ERROR, completed=False, error=err)

    run_id = _extract_run_id(run)
    if not run_id:
        return RunResult(
            triggered=True, run_id=None, status=STATUS_ERROR, completed=False,
            error="trigger response did not include a run id",
        )

    # A trigger response may already carry a terminal status (synchronous run).
    init_status = _extract_status(run)
    init_class = classify_run_status(init_status)
    if init_class == "completed":
        return RunResult(triggered=True, run_id=run_id, status=STATUS_COMPLETED, completed=True, polls=0)
    if init_class == "failed":
        return RunResult(
            triggered=True, run_id=run_id, status=STATUS_FAILED, completed=False,
            error=_extract_error(run) or f"run status: {init_status}",
        )

    # 2. Poll until terminal or until the max wait elapses.
    deadline = monotonic_fn() + max_wait_seconds
    polls = 0
    last_status: str | None = init_status
    last_error: str | None = None

    while True:
        if monotonic_fn() >= deadline:
            return RunResult(
                triggered=True, run_id=run_id, status=STATUS_TIMEOUT, completed=False,
                error=(
                    last_error
                    or f"run did not complete within {max_wait_seconds}s "
                       f"(last status: {last_status or 'unknown'})"
                ),
                polls=polls,
            )
        try:
            run = client.get_run(run_id)
            polls += 1
            last_status = _extract_status(run)
            cls = classify_run_status(last_status)
            if cls == "completed":
                return RunResult(triggered=True, run_id=run_id, status=STATUS_COMPLETED, completed=True, polls=polls)
            if cls == "failed":
                return RunResult(
                    triggered=True, run_id=run_id, status=STATUS_FAILED, completed=False,
                    error=_extract_error(run) or f"run status: {last_status}",
                    polls=polls,
                )
            # else: still running — fall through to sleep + re-poll.
        except Exception as exc:
            # Transient poll error — remember it and retry until the deadline.
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "lead_source_run_poll_error",
                extra={"run_id": run_id, "error": last_error},
            )
        sleep_fn(poll_interval_seconds)
