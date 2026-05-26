from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# (table_name, column_name, DDL fragment). Kept ordered so a fresh DB and an
# existing DB both end up identical. Each entry must be idempotent:
# `_apply_runtime_migrations` skips when the column already exists.
_RUNTIME_COLUMN_ADDS: list[tuple[str, str, str]] = [
    # Added so bulk-regen resume can fingerprint the prompt that produced
    # a row, rather than relying on the unchanging prompt_version constant.
    ("generated_contents", "prompt_fingerprint", "VARCHAR(64)"),
]


def _apply_runtime_migrations() -> None:
    """Idempotently add columns we've introduced since the table was created.

    `Base.metadata.create_all()` covers new tables but never alters existing
    ones, and we don't run Alembic in this project. The list above is the
    one source of truth for runtime-applied column additions; both SQLite
    and Postgres accept the plain `ALTER TABLE ... ADD COLUMN` form below.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table_name, col_name, ddl_type in _RUNTIME_COLUMN_ADDS:
        if table_name not in existing_tables:
            continue  # create_all just made it with the new column already in place
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        if col_name in cols:
            continue
        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {ddl_type}")
            )


def init_db() -> None:
    from src import models  # noqa: F401  registers tables on Base.metadata
    Base.metadata.create_all(engine)
    _apply_runtime_migrations()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
