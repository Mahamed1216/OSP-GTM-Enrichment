"""Bulk Push-to-Instantly execution — extracted from the Leads page so the
confirm-push flow is unit-testable (no Streamlit).

`run_bulk_push` re-runs the full server-side eligibility/safety filter, pushes
only still-eligible selected leads via `deliver_email` (which re-runs every
pre-send guard and never marks a blocked/failed lead as sent), and returns a
result record the UI persists in session_state so it survives reruns.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_bulk_push(
    selected_lead_ids: list[int],
    workspace_id: int | None,
    *,
    campaign_id: str | None,
    deliver=None,
) -> dict:
    """Push the still-eligible subset of `selected_lead_ids` to Instantly.

    Returns a result dict with status in {completed, blocked, failed} plus
    counts, blocked reasons, and per-lead failures. Never raises — exceptions
    are captured into the record. `deliver` is injectable for tests; defaults
    to src.delivery.instantly.deliver_email.
    """
    from src.db import session_scope
    from src.delivery.eligibility import filter_eligible
    from src.delivery.instantly import deliver_email

    deliver = deliver or deliver_email

    rec: dict = {
        "status": "started",
        "workspace_id": workspace_id,
        "campaign_id": campaign_id,
        "selected_count": len(selected_lead_ids),
        "eligible_count": 0,
        "n_sent": 0,
        "n_failed": 0,
        "blocked_count": 0,
        "blocked_reasons": {},
        "failures": [],
        "error": None,
        "started_at": _now(),
        "completed_at": None,
    }

    try:
        # Config guard — do not weaken safety gates.
        if not (campaign_id or "").strip():
            rec["status"] = "blocked"
            rec["error"] = "No Instantly campaign configured for this workspace."
            return rec

        # Re-run the full server-side safety/eligibility filter NOW (unsafe
        # internal content, missing/empty content, email verification, tier,
        # already-sent/in-progress, workspace scope).
        with session_scope() as session:
            fresh_eligible, fresh_skipped = filter_eligible(selected_lead_ids, session)
        sel_set = set(selected_lead_ids)
        push_ids = [lid for lid in fresh_eligible if lid in sel_set]
        rec["eligible_count"] = len(push_ids)
        rec["blocked_count"] = len(selected_lead_ids) - len(push_ids)
        rec["blocked_reasons"] = {
            code: len(ids) for code, ids in fresh_skipped.items() if ids
        }

        log.info("bulk_push_started", extra={
            "workspace_id": workspace_id,
            "selected_count": len(selected_lead_ids),
            "eligible_count": len(push_ids),
            "campaign_id": campaign_id,
            "blocked_count": rec["blocked_count"],
        })

        if not push_ids:
            rec["status"] = "blocked"
            rec["error"] = "No selected leads are still eligible — nothing was pushed."
            return rec

        for lid in push_ids:
            try:
                result = await deliver(lid, dry_run=False, workspace_id=workspace_id)
            except Exception as exc:
                rec["n_failed"] += 1
                rec["failures"].append((lid, f"{type(exc).__name__}: {exc}"))
                log.warning("bulk_push_lead_failed", extra={
                    "lead_id": lid, "error": f"{type(exc).__name__}: {exc}",
                })
            else:
                if getattr(result, "delivered", False):
                    rec["n_sent"] += 1
                else:
                    rec["n_failed"] += 1
                    rec["failures"].append(
                        (lid, f"blocked: {getattr(result, 'skip_reason', None)}")
                    )
        rec["status"] = "completed"
    except Exception as exc:  # pragma: no cover - defensive; never swallow silently
        import traceback
        rec["status"] = "failed"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        log.error("bulk_push_exception", extra={
            "workspace_id": workspace_id, "error": rec["error"],
            "traceback": traceback.format_exc(),
        })
    finally:
        rec["completed_at"] = _now()
        log.info("bulk_push_completed", extra={
            "workspace_id": workspace_id,
            "status": rec["status"],
            "push_success_count": rec["n_sent"],
            "push_failure_count": rec["n_failed"],
            "blocked_count": rec["blocked_count"],
            "blocked_reasons": rec["blocked_reasons"],
        })
    return rec
