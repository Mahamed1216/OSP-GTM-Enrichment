"""Prompt overlay loader — user-editable JSON overlay for the three
content-generation system prompts (email / linkedin_msg / call_script).

Singleton file at ``data/prompts_config.json``. Each channel's body is
stored independently so saving one does not clobber the others. Atomic
write on save (tmp + os.replace), mirroring ``src/icp_config.py`` so a
concurrent reader never sees a half-written file.

The loader returns the v4 default body whenever no overlay exists for a
channel, so a fresh checkout behaves identically to the hardcoded
constants.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_PATH = Path("data/prompts_config.json")

_VALID_CHANNELS = {"email", "linkedin_msg", "call_script"}


def load_overlay(path: Path = CONFIG_PATH) -> dict:
    """Read the overlay JSON. Returns empty dict if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            log.warning("prompts_overlay_not_a_dict", extra={"path": str(path)})
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "prompts_overlay_load_failed",
            extra={"path": str(path), "error": str(exc)},
        )
        return {}


def _atomic_write(data: dict, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def save_overlay(channel: str, prompt_text: str, path: Path = CONFIG_PATH) -> None:
    """Atomically write the overlay for one channel. Preserves the others."""
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    data = load_overlay(path)
    data[channel] = prompt_text
    _atomic_write(data, path)


def get_effective_prompt(channel: str, default: str, path: Path = CONFIG_PATH) -> str:
    """Return overlay text for this channel if present, else the default."""
    data = load_overlay(path)
    val = data.get(channel)
    if isinstance(val, str) and val.strip():
        return val
    return default


def reset_overlay(channel: str, path: Path = CONFIG_PATH) -> None:
    """Remove one channel's overlay. Other channels are preserved."""
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    data = load_overlay(path)
    if channel not in data:
        return
    data.pop(channel, None)
    if not data:
        # No overlays left — remove the file so get_last_saved_timestamp()
        # reflects "no overlay file exists" rather than an empty {}.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write(data, path)


def reset_all_overlays(path: Path = CONFIG_PATH) -> None:
    """Remove the overlay file entirely."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def get_last_saved_timestamp(path: Path = CONFIG_PATH) -> str | None:
    """Return ISO timestamp of the overlay file's mtime, or None if absent."""
    if not path.exists():
        return None
    ts = datetime.fromtimestamp(path.stat().st_mtime)
    return ts.strftime("%Y-%m-%d %H:%M:%S")
