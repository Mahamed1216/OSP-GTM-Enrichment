"""DB-backed run tracking for the internal API (api_runs table).

Small, dependency-light CRUD so POST /process can record a run and a worker
(or the inline sync path) can update it. No business logic here.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from src.db import session_scope
from src.models import ApiRun, now_utc

log = logging.getLogger(__name__)


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:20]


def _to_dict(row: ApiRun) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "workspace_id": row.workspace_id,
        "source": row.source,
        "run_mode": row.run_mode,
        "status": row.status,
        "lead_count": row.lead_count,
        "processed_count": row.processed_count,
        "failed_count": row.failed_count,
        "request_payload": row.request_payload,
        "result_payload": row.result_payload,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "completed_at": row.completed_at,
    }


def create_run(
    *,
    workspace_id: int | None,
    source: str | None,
    run_mode: str,
    request_payload: dict,
    lead_count: int,
    status: str = "queued",
) -> str:
    """Create an api_runs row and return its run_id."""
    run_id = new_run_id()
    with session_scope() as session:
        session.add(ApiRun(
            run_id=run_id,
            workspace_id=workspace_id,
            source=source,
            run_mode=run_mode,
            status=status,
            lead_count=lead_count,
            request_payload=request_payload,
        ))
    log.info(
        "api_run_created",
        extra={"run_id": run_id, "workspace_id": workspace_id, "run_mode": run_mode,
               "lead_count": lead_count, "status": status},
    )
    return run_id


def get_run(run_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.execute(
            select(ApiRun).where(ApiRun.run_id == run_id)
        ).scalar_one_or_none()
        return _to_dict(row) if row is not None else None


def mark_running(run_id: str) -> None:
    with session_scope() as session:
        row = session.execute(
            select(ApiRun).where(ApiRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is not None:
            row.status = "running"
            row.updated_at = now_utc()


def complete_run(
    run_id: str,
    *,
    status: str,
    result_payload: dict | None = None,
    processed_count: int = 0,
    failed_count: int = 0,
    error: str | None = None,
) -> None:
    """Finalize a run (status completed|failed|partial) with its results."""
    with session_scope() as session:
        row = session.execute(
            select(ApiRun).where(ApiRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        row.result_payload = result_payload
        row.processed_count = processed_count
        row.failed_count = failed_count
        row.error = error
        row.completed_at = now_utc()
        row.updated_at = now_utc()
    log.info(
        "api_run_completed",
        extra={"run_id": run_id, "status": status,
               "processed": processed_count, "failed": failed_count},
    )


def list_queued_run_ids(limit: int = 10) -> list[str]:
    """Return queued run ids oldest-first (for the async worker)."""
    with session_scope() as session:
        rows = session.execute(
            select(ApiRun.run_id)
            .where(ApiRun.status == "queued")
            .order_by(ApiRun.id.asc())
            .limit(limit)
        ).scalars().all()
        return list(rows)


def list_recent_runs(limit: int = 25, workspace_id: int | None = None) -> list[dict[str, Any]]:
    """Recent runs, newest first, for the operator console.

    Returns a compact summary — the full request/result payloads can be large,
    so they are fetched per-run via ``get_run`` instead.
    """
    with session_scope() as session:
        query = select(ApiRun).order_by(ApiRun.id.desc()).limit(max(1, min(100, limit)))
        if workspace_id is not None:
            query = query.where(ApiRun.workspace_id == workspace_id)
        return [
            {
                "run_id": row.run_id,
                "status": row.status,
                "run_mode": row.run_mode,
                "source": row.source,
                "lead_count": row.lead_count,
                "processed_count": row.processed_count,
                "failed_count": row.failed_count,
                "error": row.error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            }
            for row in session.execute(query).scalars().all()
        ]
