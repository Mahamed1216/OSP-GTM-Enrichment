"""api/requirements.txt must cover everything the Vercel function imports.

This deployment has now failed twice on a missing Python dependency, each time
surfacing only as a runtime error on the live site. These tests turn that into a
local failure: they walk the real import graph of the serverless function and
assert every third-party module it can reach is declared.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
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

# Imports the function may reach but must NOT declare. Both are guarded by
# try/except ImportError because the Streamlit UI does not run on Vercel.
_OPTIONAL_MODULES = {"streamlit", "pandas"}

# Test-only packages that belong in the root file but not the function bundle.
_TEST_ONLY = {"pytest", "pytest-asyncio"}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared(path: Path) -> dict[str, str]:
    """Return {normalized package name: version specifier} from a reqs file."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?(.*)$", line)
        if match:
            out[_normalize(match.group(1))] = match.group(3).strip()
    return out


def _imported_modules() -> dict[str, set[str]]:
    """Top-level third-party modules reachable from the serverless function."""
    stdlib = set(sys.stdlib_module_names)
    local = {"src", "app", "api", "scripts", "tests"}
    sources = [_ROOT / "api" / "index.py", _ROOT / "app" / "lib" / "config.py"]
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


def test_vercel_requirements_file_exists():
    """Vercel resolves requirements next to the function entrypoint."""
    assert _VERCEL_REQS.is_file(), (
        "api/requirements.txt is missing. Without it the Vercel Python function "
        "deploys with no third-party packages and dies with "
        "ModuleNotFoundError: No module named 'fastapi'."
    )


def test_every_backend_import_is_declared():
    declared = _declared(_VERCEL_REQS)
    missing: list[str] = []
    for module, files in sorted(_imported_modules().items()):
        if module in _OPTIONAL_MODULES:
            continue
        dist = _normalize(_DIST_FOR_MODULE.get(module, module))
        if dist not in declared:
            missing.append(f"{module} (-> {dist}) imported by {sorted(files)[0]}")
    assert not missing, (
        "api/requirements.txt is missing packages the function imports:\n  "
        + "\n  ".join(missing)
    )


def test_psycopg2_declared():
    """SQLAlchemy loads the driver by name, so no import statement reveals it."""
    assert "psycopg2-binary" in _declared(_VERCEL_REQS), (
        "psycopg2-binary must be declared or every Postgres connection fails."
    )


def test_optional_ui_packages_not_bundled():
    declared = _declared(_VERCEL_REQS)
    for name in _OPTIONAL_MODULES:
        assert _normalize(name) not in declared, (
            f"{name} must not ship in the function bundle — its import sites are "
            "guarded and it would bloat the serverless package."
        )


@pytest.mark.parametrize("package", sorted(_declared(_VERCEL_REQS)))
def test_version_specifiers_match_root_requirements(package):
    """Shared packages must not drift between the two dependency files."""
    root = _declared(_ROOT_REQS)
    if package not in root:
        pytest.skip(f"{package} is function-only")
    assert _declared(_VERCEL_REQS)[package] == root[package], (
        f"{package} is pinned differently in api/requirements.txt and "
        "requirements.txt; the function would run a different version."
    )


def test_root_runtime_packages_are_covered():
    """Anything runtime in the root file must reach the function too."""
    vercel = _declared(_VERCEL_REQS)
    missing = [
        name
        for name in _declared(_ROOT_REQS)
        if name not in _TEST_ONLY and name not in vercel
    ]
    assert not missing, (
        "these runtime packages are in requirements.txt but not in "
        f"api/requirements.txt: {missing}"
    )
