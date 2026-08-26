"""Check a deployed URL serves the UI at / and the API under /health, /api/*.

    python scripts/check_deployment.py https://osp-gtm-enrichment.vercel.app

Exists because "is the UI actually live?" was guessed at for several rounds when
one request would have answered it. `/` returning JSON means Vercel is serving
no Next.js output and every path is falling through to the Python function —
a build/settings problem, not a routing one.

Standard library only, so it runs anywhere without installing anything.
Exit code 0 = all checks passed, 1 = something is wrong.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import NamedTuple

TIMEOUT = 30


class Result(NamedTuple):
    path: str
    status: int
    content_type: str
    body: str


def fetch(base: str, path: str) -> Result | None:
    url = base.rstrip("/") + path
    request = urllib.request.Request(url, headers={"User-Agent": "osp-deploy-check"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(4096).decode("utf-8", "replace")
            return Result(path, response.status, response.headers.get("content-type", ""), body)
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a useful body
        body = exc.read(4096).decode("utf-8", "replace")
        return Result(path, exc.code, exc.headers.get("content-type", ""), body)
    except Exception as exc:
        print(f"  FAIL {path} — request failed: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    base = sys.argv[1]
    print(f"Checking {base}\n")
    failures: list[str] = []

    # 1. The homepage must be the Next.js UI.
    home = fetch(base, "/")
    if home is None:
        failures.append("/ unreachable")
    else:
        is_html = "text/html" in home.content_type.lower()
        print(f"  /            {home.status}  {home.content_type.split(';')[0]}")
        if not is_html:
            failures.append(
                f"/ returned {home.content_type or 'no content-type'} instead of HTML. "
                f"Body: {home.body[:120]!r}\n"
                "       -> Vercel is serving no Next.js output and everything is "
                "falling through to the Python function.\n"
                "       -> Check Build & Development Settings: Framework Preset must "
                "be Next.js and the Build Command / Output Directory overrides must "
                "be OFF (not set to an empty value, and Output Directory must not be "
                "'public')."
            )
        elif "OSP GTM Enrichment" not in home.body:
            failures.append("/ is HTML but does not look like the operator console")

    # 2. The API must answer with JSON.
    for path in ("/health", "/api/info"):
        result = fetch(base, path)
        if result is None:
            failures.append(f"{path} unreachable")
            continue
        print(f"  {path:12} {result.status}  {result.content_type.split(';')[0]}")
        if "application/json" not in result.content_type.lower():
            failures.append(f"{path} did not return JSON (got {result.content_type!r})")
            continue
        try:
            payload = json.loads(result.body)
        except json.JSONDecodeError:
            failures.append(f"{path} returned malformed JSON")
            continue
        if path == "/health":
            for key in ("backend_importable", "database_configured"):
                print(f"                 {key}: {payload.get(key)}")
            if payload.get("status") != "ok":
                failures.append(
                    f"/health is {payload.get('status')!r}: "
                    f"{payload.get('backend_error') or payload.get('database_error')}"
                )

    print()
    if failures:
        print(f"{len(failures)} problem(s):")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("All checks passed: / serves the UI, /health and /api/info serve JSON.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
