"""SalesOS shared-Supabase integration.

This package lets the OSP GTM Enrichment engine run as a Dockerized background
worker against the **shared SalesOS Supabase database**:

  - ``models``       — the data-contract tables (see docs/salesos_supabase_contract.md)
  - ``adapter``      — load queued jobs, convert SalesOS leads → internal shape,
                       run the EXISTING pipeline, write results back
  - ``worker``       — ``python -m src.integrations.salesos.worker`` (processing)
  - ``sending``      — approval-gated send checks
  - ``send_approved``— ``python -m src.integrations.salesos.send_approved`` (delivery)
  - ``approvals``    — approval lookups (also used by the shared eligibility gate)

It is dormant in standalone mode (``SALESOS_INTEGRATION_MODE=false``): the
contract tables are only created when ``ensure_salesos_tables()`` is called by
the integration workers/adapters.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def ensure_salesos_tables() -> bool:
    """Create the SalesOS contract tables if missing. Idempotent + safe.

    Importing ``src.integrations.salesos.models`` registers the tables on the
    shared SQLAlchemy ``Base.metadata``; ``ensure_tables`` then creates only the
    ones that don't yet exist (``checkfirst=True``). Returns True on success.
    """
    from src.db import ensure_tables
    from src.integrations.salesos.models import SALESOS_TABLES  # noqa: F401 registers tables

    return ensure_tables(*SALESOS_TABLES)
