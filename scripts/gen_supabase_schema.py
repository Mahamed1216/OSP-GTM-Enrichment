"""Generate supabase/schema.sql from the SQLAlchemy models.

The models in ``src/models.py`` are the only source of truth for this schema —
there is no Alembic, no migrations directory and no checked-in SQL. Rather than
transcribe 16 tables by hand, this emits the PostgreSQL DDL straight from
``Base.metadata`` so the file cannot drift from the code.

    python scripts/gen_supabase_schema.py

Re-run it after changing any model, and commit the result.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from src import models  # noqa: F401  registers every table on Base.metadata
from src.db import Base

OUT = _ROOT / "supabase" / "schema.sql"

# What each table is for, in one line. Emitted as COMMENT ON TABLE so the
# descriptions show up in the Supabase table editor.
TABLE_COMMENTS: dict[str, str] = {
    "workspaces": (
        "A named operating context (one per client or campaign). Every other "
        "table is scoped to a workspace via workspace_id. Required to boot."
    ),
    "leads": (
        "The contacts the pipeline works on. Unique on (email, workspace_id). "
        "Required to boot."
    ),
    "enrichments": "Enrichment result for one lead (LinkedIn, company, buyer research).",
    "scores": "ICP fit score and tier (A/B/C/D) for one lead.",
    "generated_contents": (
        "One row per generated artifact - email, call script or LinkedIn DM."
    ),
    "engagements": "Per-lead delivery and engagement events synced back from Instantly.",
    "content_ratings": "Human ratings of generated content, feeding the self-improvement loop.",
    "winning_examples": "Legacy DB-backed winners library. Superseded by the JSON library.",
    "instantly_analytics_snapshots": "Raw and parsed result of one Instantly campaign analytics poll.",
    "prompt_recommendations": "Self-improvement-loop prompt recommendation, gated on human approval.",
    "prompt_configs": (
        "User-edited overrides for the content-generation system prompts, "
        "unique per (channel, workspace_id)."
    ),
    "reply_drafts": "Reply draft produced by the Manual Draft Tester. Never sent automatically.",
    "reply_threads": "One inbound reply synced from Instantly, with its auto-generated draft.",
    "lead_signals": "Buying-intent signal discovered for a lead (hiring, imported source signals).",
    "lead_source_imports": "Audit record for one pull-based import from the external lead source API.",
    "api_runs": "Audit and async-tracking record for one internal-API process request.",
}

# Timestamps are set by the application (Python-side defaults), so the DB does
# not need them. A default is still convenient for rows inserted by hand in the
# Supabase SQL editor, and is harmless because the app always supplies a value.
TIMESTAMP_DEFAULT_COLUMNS = ("created_at", "updated_at")

HEADER = """\
-- OSP GTM Enrichment - Supabase schema
--
-- Generated from src/models.py by scripts/gen_supabase_schema.py. Do not edit
-- by hand: re-run the generator instead, or the file will drift from the code.
--
-- Safe to run on a fresh Supabase project, and safe to re-run: every statement
-- is IF NOT EXISTS / idempotent.
--
-- No extensions are required. Every primary key is a SERIAL integer; nothing in
-- this schema uses uuid or pgcrypto.
--
-- Paste the whole file into the Supabase SQL Editor and run it. See
-- supabase/README.md for the full setup, including the DATABASE_URL to give
-- Vercel.
"""

RLS = """\
-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
-- The app connects straight to Postgres as the table owner, so it bypasses RLS
-- and needs no policies. But Supabase also exposes every public table through
-- PostgREST, where the project's anon key would otherwise be able to read and
-- write this data. Enabling RLS with no policies closes that door while leaving
-- the app's direct connection working.
--
-- Only remove this if you deliberately want the tables reachable from the
-- Supabase REST/client SDKs, and then add policies rather than disabling RLS.
"""

SEED = """\
-- ---------------------------------------------------------------------------
-- Default workspace
-- ---------------------------------------------------------------------------
-- The app expects one default workspace to exist. Normally seed_default_workspace()
-- creates it, but that only runs from init_db() - which the Vercel function does
-- not call - so seed it here to make the pure-SQL path complete.
INSERT INTO workspaces (name, slug, is_default, is_active, created_at, updated_at)
SELECT 'OSP', 'osp', TRUE, TRUE, (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
WHERE NOT EXISTS (SELECT 1 FROM workspaces WHERE slug = 'osp');
"""


def main() -> None:
    dialect = postgresql.dialect()
    parts: list[str] = [HEADER]

    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip()

        # Application-side defaults mirrored into the DB for hand-inserted rows.
        for column in TIMESTAMP_DEFAULT_COLUMNS:
            needle = f"\t{column} TIMESTAMP WITHOUT TIME ZONE NOT NULL"
            if needle in ddl:
                ddl = ddl.replace(
                    needle,
                    f"{needle} DEFAULT (now() AT TIME ZONE 'utc')",
                    1,
                )

        comment = TABLE_COMMENTS.get(table.name, "")
        parts.append(
            f"\n-- ---------------------------------------------------------------------------\n"
            f"-- {table.name}\n"
            f"-- ---------------------------------------------------------------------------\n"
            f"{ddl};\n"
        )
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            parts.append(
                str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip() + ";\n"
            )
        if comment:
            escaped = comment.replace("'", "''")
            parts.append(f"COMMENT ON TABLE {table.name} IS '{escaped}';\n")

    parts.append("\n" + SEED)
    parts.append("\n" + RLS)
    for table in Base.metadata.sorted_tables:
        parts.append(f"ALTER TABLE {table.name} ENABLE ROW LEVEL SECURITY;\n")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"[OK] wrote {OUT.relative_to(_ROOT)} ({len(Base.metadata.sorted_tables)} tables)")


if __name__ == "__main__":
    main()
