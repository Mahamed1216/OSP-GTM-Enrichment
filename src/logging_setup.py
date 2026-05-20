"""Logging setup: structured JSON to file + Rich console for interactive runs.

File path is logs/sdr_run_<YYYY-MM-DD>.log so scheduled runs are debuggable.
"""
import json
import logging
from datetime import datetime

from rich.logging import RichHandler

from src.config import settings


_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info", "thread",
    "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


_initialized = False


def setup_logging(console: bool = True) -> None:
    global _initialized
    if _initialized:
        return

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_dir / f"sdr_run_{datetime.utcnow().strftime('%Y-%m-%d')}.log"

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if console:
        rich_handler = RichHandler(rich_tracebacks=True, show_time=True, show_path=False, markup=False)
        rich_handler.setLevel(settings.log_level)
        root.addHandler(rich_handler)

    _initialized = True
