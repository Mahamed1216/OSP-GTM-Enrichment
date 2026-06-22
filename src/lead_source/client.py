"""HTTP client for the OSP Lead Engine API (leads.osp.tools).

Auth: Authorization: Bearer <api_key>  (Postman-confirmed format)
Health endpoint: no auth required (spec: "No auth required")

All methods are synchronous (httpx without async) so they can be called
from Streamlit event handlers and test fixtures without an event loop.

Security: the api_key only appears in the Authorization header value.
It never appears in log output — we log only base_url and slug.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20
EXTERNAL_SOURCE = "osp_lead_engine"


class LeadSourceClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        # Bearer token format confirmed by Postman collection.
        self._auth_headers: dict[str, str] = {}
        if api_key:
            self._auth_headers["Authorization"] = f"Bearer {api_key}"

    # ------------------------------------------------------------------
    # Health — NO auth (spec: "No auth required")
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """GET /api/v1/health — liveness check, no auth needed."""
        resp = httpx.get(
            f"{self._base}/api/v1/health",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Client config — with auth
    # ------------------------------------------------------------------

    def get_client(self, slug: str) -> dict[str, Any]:
        """GET /api/v1/clients/{slug} — returns ClientOut with ICP list.

        Response: {slug, name, status, icps: [{slug, name, enabled, ...}], ...}
        """
        resp = httpx.get(
            f"{self._base}/api/v1/clients/{slug}",
            headers=self._auth_headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Sourcing runs — with auth (signal-first trigger/poll flow)
    # ------------------------------------------------------------------

    def trigger_run(self, slug: str) -> dict[str, Any]:
        """POST /api/v1/clients/{slug}/runs — start a sourcing run.

        Returns the RunOut dict (expected to carry an id/run_id and status).
        Only ever called when the workspace explicitly enables
        trigger_run_before_import — never on the default polling path.
        Raises on HTTP errors so the caller can record a failed run.
        """
        log.info(
            "lead_source_trigger_run",
            extra={"base": self._base, "slug": slug},
        )
        resp = httpx.post(
            f"{self._base}/api/v1/clients/{slug}/runs",
            headers=self._auth_headers,
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        """GET /api/v1/runs/{run_id} — poll the status of a sourcing run.

        Returns the RunOut dict (expected to carry a status field). Raises on
        HTTP errors so the poller can surface a clear failure.
        """
        resp = httpx.get(
            f"{self._base}/api/v1/runs/{run_id}",
            headers=self._auth_headers,
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Contacts — with auth
    # ------------------------------------------------------------------

    def list_contacts(
        self,
        slug: str,
        *,
        limit: int = 25,
        offset: int = 0,
        icp: str | None = None,
        status: str | None = None,
        include_suppressed: bool = False,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/clients/{slug}/contacts — returns ContactList dict.

        Response shape: {contacts: [...], count: int, limit: int, offset: int}
        API limit cap: 500.
        """
        params: dict[str, Any] = {
            "limit": min(limit, 500),
            "offset": offset,
            "include_suppressed": include_suppressed,
        }
        if icp:
            params["icp"] = icp
        if status:
            params["status"] = status
        if created_after:
            params["created_after"] = created_after
        if created_before:
            params["created_before"] = created_before

        log.info(
            "lead_source_list_contacts",
            extra={"base": self._base, "slug": slug, "limit": params["limit"], "offset": offset},
        )
        resp = httpx.get(
            f"{self._base}/api/v1/clients/{slug}/contacts",
            headers=self._auth_headers,
            params=params,
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Test connection: health (no auth) + client config (with auth)
    # ------------------------------------------------------------------

    def test_connection(self, slug: str) -> dict[str, Any]:
        """Verify connectivity and auth. Returns safe diagnostic dict.

        Step 1: GET /api/v1/health (no auth) — verifies server is up.
        Step 2: GET /api/v1/clients/{slug} (with auth) — verifies key + slug.

        Never raises — all errors are captured so the UI can display them.
        """
        result: dict[str, Any] = {
            "ok": False,
            "health_status_code": None,
            "client_status_code": None,
            "client_name": None,
            "client_status": None,
            "icp_slugs": [],
            "error": None,
        }

        # Step 1 — health check (no auth)
        try:
            resp = httpx.get(f"{self._base}/api/v1/health", timeout=10)
            result["health_status_code"] = resp.status_code
            resp.raise_for_status()
        except Exception as exc:
            result["error"] = f"Health check failed: {type(exc).__name__}: {exc}"
            log.warning(
                "lead_source_health_failed",
                extra={"base": self._base, "error": result["error"]},
            )
            return result

        # Step 2 — client config (with auth; verifies key + slug)
        try:
            resp = httpx.get(
                f"{self._base}/api/v1/clients/{slug}",
                headers=self._auth_headers,
                timeout=10,
            )
            result["client_status_code"] = resp.status_code
            resp.raise_for_status()
            data = resp.json()
            result["client_name"] = data.get("name") or data.get("slug")
            result["client_status"] = data.get("status")
            result["icp_slugs"] = [
                icp["slug"] for icp in data.get("icps", []) if icp.get("slug")
            ]
            result["ok"] = True
        except Exception as exc:
            result["error"] = f"Client config fetch failed: {type(exc).__name__}: {exc}"
            log.warning(
                "lead_source_client_config_failed",
                extra={"base": self._base, "slug": slug, "error": result["error"]},
            )

        return result
