"""Save uploaded CSV to a tempfile and shell out to scripts/ingest_leads.py.

Subprocess keeps the UI 100% off the backend code path — exact parity with the
CLI. The script emits a final `Ingest result: {json}` line that we parse for
the full counts breakdown (inserted/updated/deduped/skipped + skip reasons).

Phase 8a: also exposes auto-detect helpers for the mapping-preview UI, and
lets the caller pass an explicit canonical->source mapping that is forwarded
to the subprocess as `--mapping <tempfile.json>`.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from src.ingest_aliases import auto_detect_mapping, validate_mapping

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_INGEST_SCRIPT = _PROJECT_ROOT / "scripts" / "ingest_leads.py"
_RESULT_LINE_RE = re.compile(r"^Ingest result:\s*(\{.*\})\s*$", re.MULTILINE)


@dataclass
class IngestResult:
    inserted: int = 0
    updated: int = 0
    deduped_within_file: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def save_upload_to_tempfile(uploaded_file: IO[bytes]) -> Path:
    """Stream an uploaded file into a delete=False tempfile and return its path."""
    suffix = ".csv"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        data = uploaded_file.getbuffer() if hasattr(uploaded_file, "getbuffer") else uploaded_file.read()
        tmp.write(bytes(data))
    finally:
        tmp.close()
    return Path(tmp.name)


def read_csv_columns(csv_path: Path) -> list[str]:
    """Return the header row of a CSV without loading the full file."""
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return []


def auto_detect_and_validate(
    csv_path: Path,
) -> tuple[list[str], dict[str, str | None], bool, list[str]]:
    """Return (csv_columns, mapping, is_valid, errors) for the UI."""
    columns = read_csv_columns(csv_path)
    mapping = auto_detect_mapping(columns)
    ok, errors = validate_mapping(mapping)
    return columns, mapping, ok, errors


def _write_mapping_tempfile(mapping: dict[str, str | None], near: Path) -> Path:
    """Drop a mapping.json next to the uploaded CSV's tempfile."""
    fd, path = tempfile.mkstemp(suffix=".mapping.json", dir=str(near.parent))
    os.close(fd)
    # Keep only fields the user actually mapped to keep the file readable.
    payload = {field: src for field, src in mapping.items() if src}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return Path(path)


def run_ingest_subprocess(
    csv_path: Path,
    mapping: dict[str, str | None] | None = None,
    *,
    workspace_id: int | None = None,
) -> IngestResult:
    """Run `python scripts/ingest_leads.py <csv_path> [--mapping map.json] [--workspace-id N]`."""
    cmd = [sys.executable, str(_INGEST_SCRIPT), str(csv_path)]
    mapping_path: Path | None = None
    if mapping is not None:
        mapping_path = _write_mapping_tempfile(mapping, near=csv_path)
        cmd.extend(["--mapping", str(mapping_path)])
    if workspace_id is not None:
        cmd.extend(["--workspace-id", str(workspace_id)])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_PROJECT_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
    finally:
        if mapping_path is not None and mapping_path.exists():
            try:
                mapping_path.unlink()
            except OSError:
                pass

    stdout = proc.stdout or ""
    parsed: dict = {}
    match = _RESULT_LINE_RE.search(stdout)
    if match:
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            parsed = {}

    return IngestResult(
        inserted=int(parsed.get("inserted", 0)),
        updated=int(parsed.get("updated", 0)),
        deduped_within_file=int(parsed.get("deduped_within_file", 0)),
        skipped=int(parsed.get("skipped", 0)),
        skip_reasons=dict(parsed.get("skip_reasons", {})),
        stdout=stdout,
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )
