"""Read recent pipeline-event entries from JSONL log files."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG_DIR_DEFAULT = Path(__file__).resolve().parent.parent.parent / "logs"


def _candidate_log_files(log_dir: Path, days: int = 2) -> list[Path]:
    today = datetime.now(timezone.utc).date()
    files: list[Path] = []
    for offset in range(days):
        d = today - timedelta(days=offset)
        p = log_dir / f"sdr_run_{d.isoformat()}.log"
        if p.exists():
            files.append(p)
    return files


def parse_recent_pipeline_events(
    limit: int = 10,
    log_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent `pipeline_complete` events, newest first.

    Reads today's and yesterday's JSONL log files. Silently skips malformed lines.
    """
    log_dir = log_dir or LOG_DIR_DEFAULT
    if not log_dir.exists():
        return []

    events: list[dict[str, Any]] = []
    for path in _candidate_log_files(log_dir):
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("msg") == "pipeline_complete":
                        events.append(entry)
        except OSError:
            continue

    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return events[:limit]
