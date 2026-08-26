"""Formatting helpers for durations, timestamps, and source-status icons.

Timestamps are stored in UTC (naive `datetime.utcnow()` writes), but all
display goes through `fmt_timestamp`, which is now an alias for
`format_et`. That keeps storage untouched while every page renders
"2026-05-27 2:55 PM ET". Eastern is DST-aware via zoneinfo (America/
New_York) — no manual offset math.
"""
from __future__ import annotations

from datetime import datetime, timezone

try:  # Python 3.9+ stdlib
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — zoneinfo missing only on very old envs
    _ET = timezone(__import__("datetime").timedelta(hours=-5))


def fmt_duration_ms(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{int(ms)} ms"
    return f"{ms / 1000:.1f} s"


def format_et(value: datetime | str | None) -> str:
    """Render a UTC-stored timestamp as Eastern Time: '2026-05-27 2:55 PM ET'.

    Accepts naive datetimes (assumed UTC), tz-aware datetimes (any zone,
    converted to ET), or ISO strings. Returns '—' for None.
    """
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        # Naive timestamp — we store UTC by convention everywhere.
        value = value.replace(tzinfo=timezone.utc)
    et = value.astimezone(_ET)
    # %-I/%#I aren't portable; do the hour-strip manually.
    hour = et.strftime("%I").lstrip("0") or "12"
    return et.strftime(f"%Y-%m-%d {hour}:%M %p ET")


def fmt_timestamp(value: datetime | str | None) -> str:
    """Backward-compat alias — every page already imports this name.

    Previously rendered in UTC; now renders in Eastern via `format_et`.
    """
    return format_et(value)


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
