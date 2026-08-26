"""Routing config must never send `/` to the Python function.

`/` returning FastAPI's `{"detail":"Not Found"}` on the deployment is what these
guard against: the homepage belongs to Next.js, and only the listed backend
paths may be rewritten to the serverless function.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_VERCEL_JSON = _ROOT / "vercel.json"
_NEXT_CONFIG = _ROOT / "next.config.js"
_PAGES = _ROOT / "pages"

# Sources that would swallow "/" and hand it to the API.
_CATCH_ALL_SOURCES = {"/(.*)", "/:path*", "/(.*)/", "/", "/:path*/", "/(.+)?"}

_BACKEND_PATHS = (
    "/health",
    "/api/info",
    "/api/v1/:path*",
    "/api/instantly/:path*",
    "/api/lead-source/:path*",
)


def _vercel_rewrites() -> list[dict]:
    config = json.loads(_VERCEL_JSON.read_text(encoding="utf-8"))
    return config.get("rewrites", [])


def test_vercel_json_has_no_catch_all_rewrite():
    offenders = [r for r in _vercel_rewrites() if r.get("source") in _CATCH_ALL_SOURCES]
    assert not offenders, (
        f"vercel.json rewrites {offenders} would route / to the Python function. "
        "Only explicit backend paths may be rewritten."
    )


def test_vercel_json_has_no_legacy_routes_key():
    """`routes` disables Next.js' own routing entirely — never use it here."""
    config = json.loads(_VERCEL_JSON.read_text(encoding="utf-8"))
    assert "routes" not in config, (
        "vercel.json must not use the legacy `routes` key: it replaces Next.js "
        "routing wholesale and would break the UI. Use `rewrites`."
    )


@pytest.mark.parametrize("path", _BACKEND_PATHS)
def test_backend_paths_are_routed_to_the_function(path):
    sources = {r.get("source") for r in _vercel_rewrites()}
    assert path in sources, f"{path} is not rewritten to the API in vercel.json"


def test_every_vercel_rewrite_targets_the_function():
    for rewrite in _vercel_rewrites():
        assert rewrite.get("destination") == "/api/index", (
            f"unexpected rewrite destination: {rewrite}"
        )


def test_root_is_not_rewritten_anywhere():
    for rewrite in _vercel_rewrites():
        source = rewrite.get("source", "")
        assert source != "/", "vercel.json rewrites / to the API"
        assert not source.startswith("/:"), (
            f"{source!r} is a bare wildcard and would capture /"
        )


def test_next_config_has_no_catch_all_rewrite():
    text = _NEXT_CONFIG.read_text(encoding="utf-8")
    sources = re.findall(r'source:\s*"([^"]+)"', text)
    assert sources, "no rewrites found in next.config.js — did the format change?"
    for source in sources:
        assert source not in _CATCH_ALL_SOURCES, (
            f"next.config.js rewrites {source!r}, which would capture / "
            "and hand the homepage to the Python function."
        )
        assert source.startswith(("/health", "/api/")), (
            f"next.config.js rewrite {source!r} is not a backend path"
        )


def test_a_nextjs_homepage_exists():
    """Next.js must have a real page for `/`, or Vercel has nothing to serve."""
    candidates = [_PAGES / f"index.{ext}" for ext in ("jsx", "tsx", "js", "ts")]
    found = [p for p in candidates if p.is_file()]
    assert found, (
        "no pages/index.* found. Without a homepage route Next.js serves "
        "nothing at / and requests fall through to the API."
    )
    assert "OSP GTM Enrichment" in found[0].read_text(encoding="utf-8")


def test_homepage_does_not_use_server_side_rendering():
    """`/` must be statically prerenderable: no SSR, no env vars, no DB."""
    source = next(
        p for p in (_PAGES / "index.jsx", _PAGES / "index.tsx") if p.is_file()
    ).read_text(encoding="utf-8")
    for banned in ("getServerSideProps", "getInitialProps", "process.env"):
        assert banned not in source, (
            f"pages/index uses {banned}; the homepage must render without the "
            "backend, a database or any environment variable."
        )


def test_package_json_builds_nextjs():
    package = json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["build"] == "next build"
    assert package["scripts"]["start"] == "next start"
    for dependency in ("next", "react", "react-dom"):
        assert dependency in package["dependencies"], f"{dependency} missing"
