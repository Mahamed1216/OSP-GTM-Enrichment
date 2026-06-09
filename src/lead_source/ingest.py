"""Lead source import orchestration.

Hard constraints (same as the rest of the app):
- Never auto-sends email or pushes to Instantly.
- Never overwrites an existing lead's populated fields.
- Never crashes the full batch for a single bad row.
- Dedup is workspace-scoped: same contact allowed in different workspaces.

Dedup priority within a workspace:
  1. external_source + external_contact_id  (primary — API UUID per source system)
  2. email
  3. linkedin_url
  4. company_domain + full_name (first_name + last_name)

On match: update only empty fields (field promotion, never overwrite).
On no match with email: create new lead.
On no match without email: skip with "no_email_no_match".
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db import session_scope
from src.lead_source.client import EXTERNAL_SOURCE
from src.lead_source.mapper import has_identity, map_contact

log = logging.getLogger(__name__)

SKIP_MISSING_IDENTITY = "missing_identity"
SKIP_NO_EMAIL_NO_MATCH = "no_email_no_match"
SKIP_DUPLICATE = "skipped_duplicate"


class ImportResult:
    """Mutable accumulator returned by import_contacts()."""

    def __init__(self) -> None:
        self.fetched: int = 0
        self.created: int = 0
        self.updated: int = 0
        self.skipped: int = 0
        self.errors: int = 0
        self.skip_reasons: dict[str, int] = {}
        self.created_lead_ids: list[int] = []

    def bump_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def to_summary(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "skip_reasons": self.skip_reasons,
            "created_lead_ids": self.created_lead_ids,
        }


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------

def _find_existing(session: Session, fields: dict, workspace_id: int):
    """Return an existing Lead by dedup priority, or None."""
    from src.models import Lead

    # 1. external_source + external_contact_id (primary — most stable)
    if fields.get("external_source") and fields.get("external_contact_id"):
        row = session.execute(
            select(Lead).where(
                Lead.workspace_id == workspace_id,
                Lead.external_source == fields["external_source"],
                Lead.external_contact_id == fields["external_contact_id"],
            )
        ).scalar_one_or_none()
        if row:
            return row

    # 2. email
    if fields.get("email"):
        row = session.execute(
            select(Lead).where(
                Lead.workspace_id == workspace_id,
                Lead.email == fields["email"],
            )
        ).scalar_one_or_none()
        if row:
            return row

    # 3. linkedin_url
    if fields.get("linkedin_url"):
        row = session.execute(
            select(Lead).where(
                Lead.workspace_id == workspace_id,
                Lead.linkedin_url == fields["linkedin_url"],
            )
        ).scalar_one_or_none()
        if row:
            return row

    # 4. company_domain + full_name
    if fields.get("company_domain") and fields.get("_full_name"):
        parts = fields["_full_name"].split(" ", 1)
        fn, ln = parts[0], (parts[1] if len(parts) > 1 else "")
        if fn:
            row = session.execute(
                select(Lead).where(
                    Lead.workspace_id == workspace_id,
                    Lead.company_domain == fields["company_domain"],
                    Lead.first_name == fn,
                    Lead.last_name == ln,
                )
            ).scalar_one_or_none()
            if row:
                return row

    return None


def _update_missing_fields(lead, fields: dict) -> bool:
    """Fill empty Lead columns from fields. Returns True if anything changed."""
    scalar_map = [
        ("first_name", "first_name"),
        ("last_name", "last_name"),
        ("title", "title"),
        ("company", "company"),
        ("company_domain", "company_domain"),
        ("linkedin_url", "linkedin_url"),
        ("company_linkedin_url", "company_linkedin_url"),
        ("industry", "industry"),
        ("phone", "phone"),
        ("external_contact_id", "external_contact_id"),
        ("external_source", "external_source"),
        ("external_client_slug", "external_client_slug"),
    ]
    changed = False
    for model_attr, field_key in scalar_map:
        new_val = fields.get(field_key)
        if new_val and not getattr(lead, model_attr, None):
            setattr(lead, model_attr, new_val)
            changed = True
    # Always backfill lead_source_raw if not yet set (refresh on re-import)
    if fields.get("lead_source_raw") and not lead.lead_source_raw:
        lead.lead_source_raw = fields["lead_source_raw"]
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Core import
# ---------------------------------------------------------------------------

def import_contacts(
    contacts: list[dict[str, Any]],
    *,
    workspace_id: int,
    client_slug: str,
    import_id: int,
    external_source: str = EXTERNAL_SOURCE,
) -> ImportResult:
    """Import a list of ContactOut dicts into the leads table.

    Each contact is processed in its own session_scope so one failure
    cannot abort the rest of the batch.
    """
    from src.models import Lead

    result = ImportResult()
    result.fetched = len(contacts)

    for raw_contact in contacts:
        contact_id = raw_contact.get("id", "?")
        try:
            fields = map_contact(raw_contact)
            # Inject import-context fields so _update_missing_fields can backfill them
            fields["external_source"] = external_source
            fields["external_client_slug"] = client_slug

            # Identity gate — must have at least one usable anchor
            if not has_identity(fields):
                result.bump_skip(SKIP_MISSING_IDENTITY)
                log.info(
                    "lead_source_skip_missing_identity",
                    extra={"workspace_id": workspace_id, "contact_id": contact_id},
                )
                continue

            with session_scope() as session:
                existing = _find_existing(session, fields, workspace_id)

                if existing:
                    changed = _update_missing_fields(existing, fields)
                    if changed:
                        result.updated += 1
                    else:
                        result.bump_skip(SKIP_DUPLICATE)
                else:
                    # New lead — require email so we can store it safely
                    # (Lead.email is NOT NULL in the production schema)
                    if not fields.get("email"):
                        result.bump_skip(SKIP_NO_EMAIL_NO_MATCH)
                        log.info(
                            "lead_source_skip_no_email_no_match",
                            extra={"workspace_id": workspace_id, "contact_id": contact_id},
                        )
                        continue

                    lead = Lead(
                        workspace_id=workspace_id,
                        first_name=fields["first_name"] or "",
                        last_name=fields["last_name"] or "",
                        email=fields["email"],
                        title=fields.get("title"),
                        company=fields.get("company"),
                        company_domain=fields.get("company_domain"),
                        linkedin_url=fields.get("linkedin_url"),
                        company_linkedin_url=fields.get("company_linkedin_url"),
                        industry=fields.get("industry"),
                        phone=fields.get("phone"),
                        external_contact_id=fields.get("external_contact_id"),
                        external_source=external_source,
                        external_client_slug=client_slug,
                        lead_source_raw=fields["lead_source_raw"],
                    )
                    session.add(lead)
                    session.flush()
                    result.created += 1
                    result.created_lead_ids.append(lead.id)
                    log.debug(
                        "lead_source_lead_created",
                        extra={"lead_id": lead.id, "workspace_id": workspace_id},
                    )

        except Exception as exc:
            result.errors += 1
            log.warning(
                "lead_source_import_row_error",
                extra={
                    "workspace_id": workspace_id,
                    "contact_id": contact_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    _finish_import(import_id, result)
    return result


# ---------------------------------------------------------------------------
# Import log helpers
# ---------------------------------------------------------------------------

def start_import_log(
    workspace_id: int,
    client_slug: str,
    requested_limit: int,
    *,
    base_url: str = "",
    icp_filter: str | None = None,
    status_filter: str | None = None,
    include_suppressed: bool = False,
) -> int:
    """Create a LeadSourceImport row and return its id."""
    from src.models import LeadSourceImport
    with session_scope() as session:
        imp = LeadSourceImport(
            workspace_id=workspace_id,
            source_name=EXTERNAL_SOURCE,
            base_url=base_url or None,
            client_slug=client_slug,
            icp_filter=icp_filter or None,
            status_filter=status_filter or None,
            include_suppressed=include_suppressed,
            started_at=datetime.utcnow(),
            status="running",
            requested_limit=requested_limit,
        )
        session.add(imp)
        session.flush()
        return imp.id


def _finish_import(import_id: int, result: ImportResult) -> None:
    from src.models import LeadSourceImport
    try:
        with session_scope() as session:
            imp = session.get(LeadSourceImport, import_id)
            if imp:
                imp.finished_at = datetime.utcnow()
                imp.status = "completed"
                imp.fetched_count = result.fetched
                imp.created_count = result.created
                imp.updated_count = result.updated
                imp.skipped_count = result.skipped
                imp.error_count = result.errors
                imp.raw_summary = result.to_summary()
    except Exception as exc:
        log.warning(
            "lead_source_finish_import_failed",
            extra={"import_id": import_id, "error": str(exc)},
        )


def _fail_import(import_id: int, error_msg: str) -> None:
    from src.models import LeadSourceImport
    try:
        with session_scope() as session:
            imp = session.get(LeadSourceImport, import_id)
            if imp:
                imp.finished_at = datetime.utcnow()
                imp.status = "failed"
                imp.error_message = error_msg[:2000]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def run_import(
    workspace_id: int,
    client_slug: str,
    api_base_url: str,
    api_key: str,
    limit: int = 25,
    *,
    icp: str | None = None,
    status_filter: str | None = None,
    include_suppressed: bool = False,
    created_after: str | None = None,
    created_before: str | None = None,
) -> ImportResult:
    """Full import flow: log → paginated fetch → ingest → update workspace metadata.

    Fetches contacts in pages (API cap 500 per page) until `limit` contacts
    have been retrieved or the server has no more results.
    Raises on HTTP errors so the UI can surface a meaningful message.
    """
    from src.lead_source.client import LeadSourceClient
    from src.lead_source.settings import update_fetch_metadata

    import_id = start_import_log(
        workspace_id, client_slug, limit,
        base_url=api_base_url,
        icp_filter=icp,
        status_filter=status_filter,
        include_suppressed=include_suppressed,
    )

    try:
        client = LeadSourceClient(api_base_url, api_key)
        all_contacts: list[dict] = []
        offset = 0

        while len(all_contacts) < limit:
            page_size = min(500, limit - len(all_contacts))
            data = client.list_contacts(
                client_slug,
                limit=page_size,
                offset=offset,
                icp=icp,
                status=status_filter,
                include_suppressed=include_suppressed,
                created_after=created_after,
                created_before=created_before,
            )
            batch = data.get("contacts", [])
            all_contacts.extend(batch)
            offset += len(batch)
            if len(batch) < page_size:
                break  # server returned fewer than requested → end of results

    except Exception as exc:
        _fail_import(import_id, str(exc))
        raise

    result = import_contacts(
        all_contacts,
        workspace_id=workspace_id,
        client_slug=client_slug,
        import_id=import_id,
    )

    update_fetch_metadata(
        workspace_id,
        status="completed",
        result_count=result.created,
    )
    return result


def preview_contacts(
    workspace_id: int,
    client_slug: str,
    api_base_url: str,
    api_key: str,
    limit: int = 25,
    *,
    icp: str | None = None,
    status_filter: str | None = None,
    include_suppressed: bool = False,
    created_after: str | None = None,
    created_before: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch contacts for preview display WITHOUT importing them.

    Returns raw ContactOut dicts from the API. No leads are created,
    no import log is written, no session state is modified.
    """
    from src.lead_source.client import LeadSourceClient
    client = LeadSourceClient(api_base_url, api_key)
    data = client.list_contacts(
        client_slug,
        limit=min(limit, 500),
        icp=icp,
        status=status_filter,
        include_suppressed=include_suppressed,
        created_after=created_after,
        created_before=created_before,
    )
    return data.get("contacts", [])


def get_recent_imports(workspace_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent import records for a workspace, newest first."""
    from src.models import LeadSourceImport
    with session_scope() as session:
        rows = session.execute(
            select(LeadSourceImport)
            .where(LeadSourceImport.workspace_id == workspace_id)
            .order_by(LeadSourceImport.started_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "client_slug": r.client_slug,
                "icp_filter": r.icp_filter,
                "status_filter": r.status_filter,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "status": r.status,
                "requested_limit": r.requested_limit,
                "fetched_count": r.fetched_count,
                "created_count": r.created_count,
                "updated_count": r.updated_count,
                "skipped_count": r.skipped_count,
                "error_count": r.error_count,
                "error_message": r.error_message,
                "raw_summary": r.raw_summary,
            }
            for r in rows
        ]
