"""The Vercel function's dependencies must be declared where uv reads them.

This deployment has failed three times on Python dependency declaration, each
time surfacing only as an error on the live site:

  1. requirements.txt was complete, but `[project]` in pyproject.toml had no
     `dependencies` key, so uv installed nothing -> "No module named 'fastapi'".
  2. Adding api/requirements.txt changed nothing; uv never looked at it.
  3. Deleting `[project]` broke `uv lock` outright -> the build itself failed.

Vercel builds with uv, so **pyproject.toml is authoritative**. These tests walk
the function's real import graph and assert every third-party module it can
reach is declared there, and that the two requirements.txt files agree with it.
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_VERCEL_REQS = _ROOT / "api" / "requirements.txt"
_ROOT_REQS = _ROOT / "requirements.txt"

# Import name -> distribution name, where they differ.
_DIST_FOR_MODULE = {
    "apify_client": "apify-client",
    "dotenv": "python-dotenv",
    "pydantic_settings": "pydantic-settings",
    "tavily": "tavily-python",
    "starlette": "fastapi",  # ships as a fastapi dependency
}

# Must never appear in the bundle. The Streamlit UI was removed from this repo;
# these guard against it (or its heavy deps) creeping back into the function.
_OPTIONAL_MODULES = {"streamlit", "pandas"}

# Test-only packages: they belong in requirements.txt but not in the bundle.
_TEST_ONLY = {"pytest", "pytest-asyncio"}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _split_requirement(spec: str) -> tuple[str, str]:
    """'fastapi>=0.111.0,<1.0' -> ('fastapi', '>=0.111.0,<1.0')."""
    match = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?(.*)$", spec.strip())
    if not match:
        return _normalize(spec), ""
    return _normalize(match.group(1)), match.group(3).strip()


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _pyproject_deps() -> dict[str, str]:
    project = _pyproject().get("project", {})
    return dict(_split_requirement(dep) for dep in project.get("dependencies", []))


def _requirements(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, spec = _split_requirement(line)
        out[name] = spec
    return out


def _imported_modules() -> dict[str, set[str]]:
    """Top-level third-party modules reachable from the serverless function."""
    stdlib = set(sys.stdlib_module_names)
    local = {"src", "app", "api", "scripts", "tests"}
    sources = [_ROOT / "api" / "index.py"]
    sources += [p for p in (_ROOT / "src").rglob("*.py") if "__pycache__" not in str(p)]

    found: dict[str, set[str]] = {}
    for file in sources:
        tree = ast.parse(file.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name and name not in stdlib and name not in local:
                    found.setdefault(name, set()).add(
                        str(file.relative_to(_ROOT)).replace("\\", "/")
                    )
    return found


# ---------------------------------------------------------------------------
# pyproject.toml — what Vercel's uv actually reads
# ---------------------------------------------------------------------------

def test_project_table_exists():
    """`uv lock` fails outright without it: 'No `project` table found'."""
    assert "project" in _pyproject(), (
        "pyproject.toml must keep a [project] table — Vercel runs `uv lock`, "
        "which refuses to run without one and fails the whole build."
    )


def test_project_declares_dependencies():
    """A [project] table with no dependencies makes uv install nothing."""
    deps = _pyproject_deps()
    assert deps, (
        "[project].dependencies is empty. uv reads this file instead of "
        "requirements.txt, so an empty list deploys a function with no "
        "third-party packages at all."
    )
    assert "fastapi" in deps


def test_no_build_system():
    """Without [build-system] uv treats this as a virtual project.

    Adding one makes uv build the repo as a package, and the flat layout
    (src/, app/, api/, scripts/, tests/) breaks setuptools auto-discovery.
    """
    assert "build-system" not in _pyproject(), (
        "Adding [build-system] makes uv try to build this repo as a package; "
        "the flat layout will fail setuptools' auto-discovery."
    )


def test_every_backend_import_is_declared_in_pyproject():
    declared = _pyproject_deps()
    missing = []
    for module, files in sorted(_imported_modules().items()):
        if module in _OPTIONAL_MODULES:
            continue
        dist = _normalize(_DIST_FOR_MODULE.get(module, module))
        if dist not in declared:
            missing.append(f"{module} (-> {dist}) imported by {sorted(files)[0]}")
    assert not missing, (
        "[project].dependencies is missing packages the function imports:\n  "
        + "\n  ".join(missing)
    )


def test_psycopg2_declared():
    """SQLAlchemy loads the driver by name, so no import statement reveals it."""
    assert "psycopg2-binary" in _pyproject_deps(), (
        "psycopg2-binary must be declared or every Postgres connection fails."
    )


def test_optional_ui_packages_not_bundled():
    declared = _pyproject_deps()
    for name in _OPTIONAL_MODULES:
        assert _normalize(name) not in declared, (
            f"{name} must not ship in the function bundle — the Streamlit UI "
            "was removed and this would only bloat the serverless package."
        )


def test_test_only_packages_not_bundled():
    declared = _pyproject_deps()
    for name in _TEST_ONLY:
        assert name not in declared, f"{name} is test-only; keep it out of the bundle."


# ---------------------------------------------------------------------------
# The requirements.txt files must not drift from pyproject.toml
# ---------------------------------------------------------------------------

def test_vercel_requirements_file_exists():
    assert _VERCEL_REQS.is_file()


@pytest.mark.parametrize("package", sorted(_pyproject_deps()))
def test_requirements_files_match_pyproject(package):
    pyproject = _pyproject_deps()
    for path in (_ROOT_REQS, _VERCEL_REQS):
        declared = _requirements(path)
        assert package in declared, (
            f"{package} is in [project].dependencies but missing from "
            f"{path.relative_to(_ROOT)}"
        )
        assert declared[package] == pyproject[package], (
            f"{package} is pinned differently in {path.relative_to(_ROOT)} "
            f"({declared[package]}) and pyproject.toml ({pyproject[package]})"
        )


def test_root_runtime_packages_are_declared():
    """Anything runtime in requirements.txt must reach the deployment too."""
    pyproject = _pyproject_deps()
    missing = [
        name
        for name in _requirements(_ROOT_REQS)
        if name not in _TEST_ONLY and name not in pyproject
    ]
    assert not missing, (
        "these runtime packages are in requirements.txt but not in "
        f"[project].dependencies: {missing}"
    )
