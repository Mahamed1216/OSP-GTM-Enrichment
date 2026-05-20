"""Initialize the SQLite database. Idempotent — safe to re-run."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.logging_setup import setup_logging


def main() -> None:
    setup_logging()
    init_db()
    print("DB initialized.")


if __name__ == "__main__":
    main()
