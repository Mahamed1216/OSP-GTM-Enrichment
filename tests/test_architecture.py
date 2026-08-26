"""The repo is a standalone Vercel + Supabase app.

The SalesOS handoff, the Streamlit operator UI, and the ECS/Docker/AWS
deployment path were all removed. These tests fail if any of them creeps back
in, because each one previously reached into the pipeline code and would
quietly change how the app deploys or sends.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PY_SOURCES = [
    p for p in list((_ROOT / "src").rglob("*.py")) + list((_ROOT / "api").rglob("*.py"))
    if "__pycache__" not in str(p)
]

# Paths that belonged to the old architecture and must stay gone.
_REMOVED_PATHS = [
    "app",                              # Streamlit UI
    "src/integrations/salesos",         # SalesOS contract layer
    "aws",                              # ECS task definitions
    ".streamlit",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    "requirements-ui.txt",
    "run_webhook.py",
    ".github/workflows/deploy-ecs.yml",
    "docs/salesos_supabase_contract.md",
]

_FORBIDDEN_IMPORTS = {
    "streamlit": "the Streamlit UI was removed; it cannot run on Vercel",
    "pandas": "pandas is not in the serverless bundle — return plain dicts",
}


def _imported_names(path: Path) -> set[str]:
    """Top-level module names imported at MODULE scope (not inside functions)."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names: set[str] = set()
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("relpath", _REMOVED_PATHS)
def test_old_architecture_paths_are_gone(relpath):
    assert not (_ROOT / relpath).exists(), (
        f"{relpath} belongs to the removed SalesOS/Streamlit/ECS architecture."
    )


@pytest.mark.parametrize("path", _PY_SOURCES, ids=lambda p: str(p.name))
def test_no_module_level_forbidden_imports(path):
    imported = _imported_names(path)
    for name, why in _FORBIDDEN_IMPORTS.items():
        assert name not in imported, f"{path.relative_to(_ROOT)} imports {name}: {why}"


def test_no_salesos_references_in_active_code():
    offenders = []
    for path in _PY_SOURCES:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "salesos" in text:
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"SalesOS references remain in: {offenders}"


def test_no_streamlit_references_in_active_code():
    offenders = []
    for path in _PY_SOURCES:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "streamlit" in text:
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"Streamlit references remain in: {offenders}"


def test_settings_has_no_salesos_flag():
    from src.config import Settings

    assert not any("salesos" in name for name in Settings.model_fields), (
        "Settings still carries a SalesOS field."
    )


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = {
    "workspaces", "leads", "enrichments", "scores", "generated_contents",
    "engagements", "content_ratings", "winning_examples",
    "instantly_analytics_snapshots", "prompt_recommendations", "prompt_configs",
    "reply_drafts", "reply_threads", "lead_signals", "lead_source_imports",
    "api_runs",
}


def test_models_define_exactly_the_standalone_tables():
    from src import models  # noqa: F401  registers the tables
    from src.db import Base

    assert set(Base.metadata.tables) == _EXPECTED_TABLES


def test_schema_sql_has_no_salesos_tables():
    sql = (_ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8").lower()
    assert "salesos" not in sql, "supabase/schema.sql still creates salesos_* tables"


def test_schema_sql_covers_every_model_table():
    sql = (_ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8").lower()
    for table in sorted(_EXPECTED_TABLES):
        assert f"create table if not exists {table} " in sql, (
            f"supabase/schema.sql does not create {table}"
        )


# ---------------------------------------------------------------------------
# Deployment surface
# ---------------------------------------------------------------------------

def test_vercel_include_files_only_names_existing_dirs():
    config = json.loads((_ROOT / "vercel.json").read_text(encoding="utf-8"))
    include = config["functions"]["api/index.py"]["includeFiles"]
    inner = include[include.index("{") + 1: include.index("}")]
    for directory in inner.split(","):
        assert (_ROOT / directory.strip()).is_dir(), (
            f"vercel.json includeFiles names {directory!r}, which does not exist"
        )


def test_env_example_lists_only_current_variables():
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    for gone in ("SALESOS", "STREAMLIT", "APP_PASSWORD", "AUTH_REQUIRED", "AWS_"):
        assert gone not in text, f".env.example still documents {gone}"
    for needed in ("DATABASE_URL", "INTERNAL_API_KEY", "ANTHROPIC_API_KEY"):
        assert needed in text, f".env.example is missing {needed}"
