"""Formatting helpers for durations, timestamps, and source-status icons."""
from __future__ import annotations

from datetime import datetime, timezone


def fmt_duration_ms(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{int(ms)} ms"
    return f"{ms / 1000:.1f} s"


def fmt_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def source_status_icon(success: bool | None) -> str:
    if success is True:
        return "✅"
    if success is False:
        return "❌"
    return "⚪"


_STATUS_ICONS = {
    "ok": "✅",
    "no_results": "⚠️",
    "skipped": "⚠️",
    "error": "❌",
}
_STATUS_LABELS = {
    "ok": "success",
    "no_results": "no results",
    "skipped": "skipped",
    "error": "error",
}


def source_status_display(meta: dict | None) -> tuple[str, str]:
    """Resolve (icon, label) for a source_status entry.

    Falls back to legacy `success` bool for rows persisted before the
    tri-state classifier (no `status` key yet).
    """
    if not meta:
        return "⚪", "unknown"
    status = meta.get("status")
    if status in _STATUS_ICONS:
        return _STATUS_ICONS[status], _STATUS_LABELS[status]
    legacy = meta.get("success")
    if legacy is True:
        return "✅", "success"
    if legacy is False:
        return "❌", "error"
    return "⚪", "unknown"
