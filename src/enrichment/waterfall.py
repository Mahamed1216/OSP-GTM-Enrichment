"""Waterfall enrichment: fan out all sources for one lead, fail-graceful.

Uses asyncio.gather(return_exceptions=True) so a single source failure never
blocks the pipeline. Per-source status/duration is persisted on the
Enrichment row's source_status JSON for observability.

Status taxonomy (see _classify):
- "ok"         — source ran and returned non-empty data
- "no_results" — source ran but returned an empty payload (yellow warning)
- "skipped"    — source short-circuited without doing real work
                 (missing input, or duration below SKIP_THRESHOLD_MS)
- "error"      — source raised; payload is None
"""
import logging
import time
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from src.db import session_scope
from src.enrichment.buyer_accounts import discover_buyer_accounts
from src.enrichment.company_details import fetch_company_details
from src.enrichment.linkedin_profile import fetch_linkedin_profile
from src.enrichment.news import fetch_company_news, fetch_industry_news
from src.icp_config import load_icp_config
from src.models import Enrichment, Lead

log = logging.getLogger(__name__)

# A source returning in under this many ms almost certainly short-circuited
# (e.g. empty-URL guard) rather than actually calling Apify/Tavily.
SKIP_THRESHOLD_MS = 100


def _is_empty_payload(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict, str)) and len(value) == 0:
        return True
    return False


def _classify(
    *,
    success: bool,
    duration_ms: int,
    payload: Any,
    input_present: bool,
) -> tuple[str, str | None]:
    """Return (status, reason). status is one of ok|no_results|skipped|error.

    If the source returned data we trust it (status="ok") regardless of
    duration — cached/quick responses are still real successes. Skip
    detection only fires when the payload is also empty, so an instant
    no-op (e.g. missing input → empty list) does not masquerade as success.
    """
    if not success:
        return "error", None  # error string lives in meta["error"]
    if not _is_empty_payload(payload):
        return "ok", None
    if not input_present:
        return "skipped", "no input provided"
    if duration_ms < SKIP_THRESHOLD_MS:
        return "skipped", f"did not execute (returned in {duration_ms}ms)"
    return "no_results", "ran but returned no items"


async def _timed(name: str, coro: Awaitable[Any]) -> tuple[str, Any, dict]:
    start = time.monotonic()
    try:
        result = await coro
        duration_ms = int((time.monotonic() - start) * 1000)
        return name, result, {"success": True, "error": None, "duration_ms": duration_ms}
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        log.warning("enrichment_source_failed", extra={"source": name, "error": str(exc), "duration_ms": duration_ms})
        return name, None, {"success": False, "error": f"{type(exc).__name__}: {exc}", "duration_ms": duration_ms}


def _model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_model_dump(v) for v in value]
    return value


async def enrich_lead(lead_id: int) -> dict:
    """Run all enrichment sources for one lead. Returns the source_status map.

    Persists results to the Enrichment row (upsert).
    """
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        snapshot = {
            "linkedin_url": lead.linkedin_url,
            "company_linkedin_url": lead.company_linkedin_url,
            "company": lead.company,
            "industry": lead.industry,
        }

    import asyncio

    # Load ICP once per enrichment so both news fetchers see the same snapshot
    # and a save mid-run can't split which lead's news cited which value prop.
    icp = load_icp_config()
    # Industry news is now "input_present" as long as the user has news_search_terms
    # configured, even if the lead has no industry field — terms drive the query.
    industry_input_present = bool(snapshot["industry"]) or bool(icp.news_search_terms)

    # Track which sources had real input so we can mark them "skipped" when missing.
    sources: list[tuple[str, Awaitable[Any], bool]] = [
        ("linkedin_profile", fetch_linkedin_profile(snapshot["linkedin_url"] or ""), bool(snapshot["linkedin_url"])),
        ("company_details", fetch_company_details(snapshot["company_linkedin_url"] or ""), bool(snapshot["company_linkedin_url"])),
        ("company_news", fetch_company_news(snapshot["company"] or "", icp=icp), bool(snapshot["company"])),
        ("industry_news", fetch_industry_news(snapshot["industry"] or "", icp=icp), industry_input_present),
        # Buyer-account discovery uses company name + industry, so input
        # is "present" iff we have a company name. The discover function
        # never raises, but a missing Tavily/Anthropic key surfaces as
        # a low-confidence empty result which classify() will mark
        # "no_results" — that's the correct status.
        (
            "buyer_accounts",
            discover_buyer_accounts(
                snapshot["company"] or "",
                industry=snapshot["industry"],
            ),
            bool(snapshot["company"]),
        ),
    ]
    input_present_by_name = {name: present for name, _, present in sources}

    results = await asyncio.gather(*[_timed(name, coro) for name, coro, _ in sources])

    payload: dict[str, Any] = {}
    status: dict[str, dict] = {}
    for name, value, meta in results:
        dumped = _model_dump(value)
        payload[name] = dumped
        cls, reason = _classify(
            success=meta["success"],
            duration_ms=meta["duration_ms"],
            payload=dumped,
            input_present=input_present_by_name[name],
        )
        meta["status"] = cls
        meta["reason"] = reason
        # `success` now means "ran and returned data" so the UI cannot render
        # an empty payload as green. Callers that relied on the old meaning
        # (no exception raised) should switch to `status != "error"`.
        meta["success"] = cls == "ok"
        if cls in ("skipped", "no_results"):
            log.info(
                "enrichment_source_no_data",
                extra={"source": name, "status": cls, "reason": reason, "duration_ms": meta["duration_ms"]},
            )
        status[name] = meta

    with session_scope() as session:
        existing = session.execute(
            select(Enrichment).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none()
        if existing:
            existing.linkedin_profile = payload.get("linkedin_profile")
            existing.company_details = payload.get("company_details")
            existing.company_news = payload.get("company_news")
            existing.industry_news = payload.get("industry_news")
            existing.buyer_accounts = payload.get("buyer_accounts")
            existing.source_status = status
        else:
            session.add(Enrichment(
                lead_id=lead_id,
                linkedin_profile=payload.get("linkedin_profile"),
                company_details=payload.get("company_details"),
                company_news=payload.get("company_news"),
                industry_news=payload.get("industry_news"),
                buyer_accounts=payload.get("buyer_accounts"),
                source_status=status,
            ))

    successes = sum(1 for s in status.values() if s["status"] == "ok")
    log.info("enrichment_complete", extra={
        "lead_id": lead_id,
        "successes": successes,
        "no_results": sum(1 for s in status.values() if s["status"] == "no_results"),
        "skipped": sum(1 for s in status.values() if s["status"] == "skipped"),
        "errors": sum(1 for s in status.values() if s["status"] == "error"),
        "total": len(status),
    })
    return status


# ---- Convenience: callable type signature for fan-out helpers ---------
EnrichmentFn = Callable[..., Awaitable[Any]]
