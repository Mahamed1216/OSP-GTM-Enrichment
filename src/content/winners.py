"""Load + format winning email examples for few-shot.

Phase 5 added content-type-aware sibling loaders/formatters and a negative-example
library that gets rendered with explicit anti-pattern framing. The legacy
`load_top_winners` and `format_winners` helpers are kept untouched for backward
compatibility (used by tests and by callers that haven't migrated yet).
"""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

WINNERS_PATH = Path("data/winning_examples.json")
NEGATIVES_PATH = Path("data/negative_examples.json")

# Locked anti-pattern framing — verbatim wording matters; tests assert on it.
NEGATIVE_FRAMING_HEADER = (
    "## Low-performing examples — DO NOT write like these. Avoid the patterns "
    "shown below."
)
WINNER_FRAMING_HEADER = "## High-performing examples (study these)"

# The only evidence that makes an email a valid winner example. Any entry
# in the Winners Library that cannot be traced to one of these reasons
# should be deactivated, not deleted.
VALID_WINNER_REASONS: frozenset[str] = frozenset({
    "seed",                  # manually seeded by operator
    "manual_positive_rating",# SDR thumbs-up in the UI
    "positive_reply",        # email received a confirmed positive reply
    "opportunity",           # lead moved to opportunity status in Instantly
    "booked_meeting",        # meeting booked as a result of this email
    "conversion",            # full conversion recorded
})

# Reason assigned to engagement_reply entries that cannot be confirmed
# as positive — kept on the entry for audit but treated as invalid.
WINNER_REASON_UNCONFIRMED = "engagement_reply_unconfirmed"


def load_top_winners(k: int = 3) -> list[dict]:
    if not WINNERS_PATH.exists():
        return []
    try:
        examples = json.loads(WINNERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("winners_load_failed", extra={"error": str(exc)})
        return []
    examples.sort(
        key=lambda e: (e.get("manually_flagged", False), e.get("reply_rate", 0.0)),
        reverse=True,
    )
    return examples[:k]


def format_winners(winners: list[dict]) -> str:
    if not winners:
        return ""
    lines = ["# Examples of emails that earned replies"]
    for i, w in enumerate(winners, 1):
        ctx = w.get("lead_context", {})
        lines.append(
            f"\n## Example {i} — {ctx.get('title', 'unknown role')} "
            f"in {ctx.get('industry', 'unknown industry')}"
        )
        if ctx.get("signal"):
            lines.append(f"Signal cited: {ctx['signal']}")
        lines.append(f"Subject: {w.get('subject', '')}")
        lines.append(f"Body:\n{w.get('body', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 5 additive helpers
# ---------------------------------------------------------------------------

def _load_json_array(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("examples_load_failed", extra={"path": str(path), "error": str(exc)})
        return []
    return items if isinstance(items, list) else []


def _derive_winner_reason(entry: dict) -> str | None:
    """Derive the winner_reason for an entry without touching the file.

    Priority:
      1. Explicit `winner_reason` field (set by new code or cleanup).
      2. `source == "seed"` or `manually_flagged == True` → "seed".
      3. `source == "manual_rating"` → "manual_positive_rating".
      4. `source == "engagement_reply"` without an explicit valid reason → None
         (unconfirmed; caller decides whether to keep or deactivate).

    Returns None when no valid reason can be determined — callers must NOT
    use this entry as a few-shot example.
    """
    reason = entry.get("winner_reason")
    if reason and reason in VALID_WINNER_REASONS:
        return reason
    source = entry.get("source") or ""
    if source == "seed" or bool(entry.get("manually_flagged", False)):
        return "seed"
    if source == "manual_rating":
        return "manual_positive_rating"
    # engagement_reply: no valid reason derivable without DB confirmation.
    return None


def _entry_score(e: dict) -> float:
    """Sort key for winners: prefer `score`, fall back to legacy `reply_rate`."""
    if "score" in e and e["score"] is not None:
        try:
            return float(e["score"])
        except (TypeError, ValueError):
            pass
    return float(e.get("reply_rate", 0.0) or 0.0)


def _entry_content_type(e: dict) -> str | None:
    """Best-effort content_type extraction; legacy seed entries default to email."""
    ct = e.get("content_type")
    if ct:
        return ct
    # Legacy entries pre-Phase-5 are all emails.
    if "subject" in e or "body" in e:
        return "email"
    return None


def list_all_winners(workspace_id: int | None = None) -> list[dict]:
    """Full winners array with derived winner_reason added — for the library
    table where the operator needs to see active + inactive entries side by side.

    Phase 4: when workspace_id is given and is not the default OSP workspace,
    loads from the WinningExample DB table for that workspace instead of the
    JSON file. This gives new workspaces an empty winners library by default.

    Does NOT mutate the file — winner_reason is derived inline for display only.
    """
    if workspace_id is not None:
        try:
            from src.workspace import get_default_workspace_id
            default_id = get_default_workspace_id()
            if default_id is None or workspace_id != default_id:
                return _list_db_winners(workspace_id)
        except Exception:
            pass
    items = _load_json_array(WINNERS_PATH)
    for item in items:
        if "winner_reason" not in item:
            item["winner_reason"] = _derive_winner_reason(item)
    return items


def _list_db_winners(workspace_id: int) -> list[dict]:
    """Load winners from WinningExample DB rows for a specific workspace."""
    try:
        from sqlalchemy import select
        from src.db import session_scope
        from src.models import WinningExample
        with session_scope() as session:
            rows = session.execute(
                select(WinningExample).where(WinningExample.workspace_id == workspace_id)
            ).scalars().all()
            result = []
            for row in rows:
                result.append({
                    "lead_context": row.lead_context or {},
                    "subject": row.subject,
                    "body": row.body,
                    "reply_rate": row.reply_rate,
                    "manually_flagged": row.manually_flagged,
                    "winner_reason": "seed",
                    "is_active": True,
                })
            return result
    except Exception as exc:
        log.warning("db_winners_load_failed", extra={"error": str(exc)})
        return []


def list_all_negatives() -> list[dict]:
    """Full negatives array, no filtering — parallel to ``list_all_winners``."""
    return _load_json_array(NEGATIVES_PATH)


def _is_osp_workspace(workspace_id: int | None) -> bool:
    """Return True if workspace_id is the default OSP workspace (or None)."""
    if workspace_id is None:
        return True
    try:
        from src.workspace import get_default_workspace_id
        return workspace_id == get_default_workspace_id()
    except Exception:
        return True


def _load_db_winners_for(content_type: str, workspace_id: int, k: int) -> list[dict]:
    """Load top-k winners from the WinningExample DB table for a workspace."""
    try:
        from sqlalchemy import select
        from src.db import session_scope
        from src.models import WinningExample
        with session_scope() as session:
            rows = session.execute(
                select(WinningExample)
                .where(
                    WinningExample.workspace_id == workspace_id,
                    # content_type NULL treated as "email" (legacy rows)
                    (WinningExample.content_type == content_type)
                    | (
                        (WinningExample.content_type.is_(None))
                        & (content_type == "email")
                    ),
                )
                .order_by(WinningExample.reply_rate.desc())
                .limit(k)
            ).scalars().all()
            return [
                {
                    "lead_context": row.lead_context or {},
                    "subject": row.subject or "",
                    "body": row.body or "",
                    "reply_rate": row.reply_rate,
                    "manually_flagged": row.manually_flagged,
                    "winner_reason": "seed",
                    "is_active": True,
                    "content_type": row.content_type or "email",
                }
                for row in rows
            ]
    except Exception as exc:
        log.warning("db_winners_for_load_failed", extra={"error": str(exc)})
        return []


def load_top_winners_for(content_type: str, k: int = 3, *, workspace_id: int | None = None) -> list[dict]:
    """Return top-k winners for a content type, scoped to the given workspace.

    Phase 6: fully workspace-scoped via WinningExample DB table.
    - Non-OSP workspaces: DB only; empty if no winners migrated/copied.
    - OSP workspace (workspace_id=None or default): DB first (migrated from JSON),
      falls back to JSON if DB is empty for backward compatibility.

    Only includes entries that are:
      - active (is_active != False)
      - have a valid winner_reason
    """
    if not _is_osp_workspace(workspace_id):
        return _load_db_winners_for(content_type, workspace_id, k)  # type: ignore[arg-type]

    # OSP: try DB first (post-migration), fall back to JSON if empty.
    if workspace_id is not None:
        db_items = _load_db_winners_for(content_type, workspace_id, k)
        if db_items:
            return db_items

    # JSON fallback (OSP legacy or pre-migration).
    items = [
        e for e in _load_json_array(WINNERS_PATH)
        if _entry_content_type(e) == content_type
        and e.get("is_active", True)
        and _derive_winner_reason(e) in VALID_WINNER_REASONS
    ]
    items.sort(
        key=lambda e: (e.get("manually_flagged", False), _entry_score(e)),
        reverse=True,
    )
    return items[:k]


def load_top_negatives(content_type: str, k: int = 2, *, workspace_id: int | None = None) -> list[dict]:
    """Return top-k negative examples for a content type.

    Phase 6: negatives are OSP-specific (from JSON). Non-OSP workspaces
    get an empty list — they have no negative examples yet.
    """
    if not _is_osp_workspace(workspace_id):
        return []

    items = [
        e for e in _load_json_array(NEGATIVES_PATH)
        if e.get("content_type") == content_type
        and e.get("is_active", True)
    ]
    items.sort(key=lambda e: e.get("added_at", ""), reverse=True)
    return items[:k]


def _entry_subject_body(e: dict) -> tuple[str | None, str]:
    """Pull subject/body from either the new `content` block or legacy top-level keys."""
    content = e.get("content") or {}
    subject = content.get("subject") if isinstance(content, dict) else None
    body = content.get("body") if isinstance(content, dict) else None
    if subject is None and "subject" in e:
        subject = e.get("subject")
    if not body:
        body = e.get("body", "")
    return subject, body or ""


def _render_call_script_body(body_str: str) -> str:
    """call_script bodies are JSON-encoded structured fields; render readably."""
    try:
        d = json.loads(body_str)
    except (TypeError, ValueError, json.JSONDecodeError):
        return body_str
    parts: list[str] = []
    if d.get("opener"):
        parts.append(f"Opener: {d['opener']}")
    if d.get("value_prop"):
        parts.append(f"Value prop: {d['value_prop']}")
    objs = d.get("objections") or []
    if objs:
        parts.append("Objections:")
        for o in objs:
            parts.append(f"  - {o.get('objection', '')}")
            parts.append(f"    → {o.get('response', '')}")
    if d.get("close"):
        parts.append(f"Close: {d['close']}")
    return "\n".join(parts) if parts else body_str


def _render_example_body(content_type: str, body_str: str) -> str:
    if content_type == "call_script":
        return _render_call_script_body(body_str)
    return body_str


def format_winners_for(content_type: str, winners: list[dict]) -> str:
    """Content-type-aware few-shot rendering with the locked positive framing.

    Returns "" when `winners` is empty so the caller can omit the section
    entirely (no orphan headers in the prompt).
    """
    if not winners:
        return ""
    lines = [WINNER_FRAMING_HEADER]
    for i, w in enumerate(winners, 1):
        ctx = w.get("lead_context") or {}
        ctx_summary = w.get("lead_context_summary")
        header_bits = [f"### Example {i}"]
        role = ctx.get("title")
        industry = ctx.get("industry")
        if role or industry:
            header_bits.append(f"— {role or 'unknown role'} in {industry or 'unknown industry'}")
        lines.append(" ".join(header_bits))
        if ctx.get("signal"):
            lines.append(f"Signal cited: {ctx['signal']}")
        elif ctx_summary:
            lines.append(f"Context: {ctx_summary}")
        subject, body = _entry_subject_body(w)
        if subject:
            lines.append(f"Subject: {subject}")
        lines.append("Body:")
        lines.append(_render_example_body(content_type, body))
        lines.append("")  # blank line between examples
    return "\n".join(lines).rstrip() + "\n"


def _set_entry_active(path: Path, entry_id: str, active: bool) -> bool:
    """Locate the entry whose ``id`` matches and flip ``is_active``.

    Atomic write via ``tmp`` + ``os.replace`` so a concurrent reader can
    never see a half-written file. Returns True if the entry was found
    and the file was rewritten, False if no such id existed (no-op).
    Mirrors the atomic-write pattern from
    :func:`src.icp_config.save_icp_config`.
    """
    import os

    items = _load_json_array(path)
    changed = False
    for entry in items:
        if entry.get("id") == entry_id:
            if entry.get("is_active", True) != active:
                entry["is_active"] = active
                changed = True
            else:
                # Already in target state — nothing to do.
                return False
            break
    else:
        return False

    if not changed:
        return False

    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(items, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    return True


def set_winner_active(entry_id: str, active: bool) -> bool:
    """Demote (``active=False``) or restore (``active=True``) a winner.

    Used by the Engagement page's library table. Returns True on a real
    write, False if no entry with that ``id`` exists or the flag was
    already in the requested state.
    """
    return _set_entry_active(WINNERS_PATH, entry_id, active)


def set_negative_active(entry_id: str, active: bool) -> bool:
    """Same as :func:`set_winner_active` but operates on the negatives library."""
    return _set_entry_active(NEGATIVES_PATH, entry_id, active)


def format_negatives(content_type: str, negatives: list[dict]) -> str:
    """Anti-pattern framing — empty input collapses the whole section."""
    if not negatives:
        return ""
    lines = [NEGATIVE_FRAMING_HEADER]
    for i, n in enumerate(negatives, 1):
        lines.append(f"### Anti-example {i}")
        ctx_summary = n.get("lead_context_summary")
        if ctx_summary:
            lines.append(f"Context: {ctx_summary}")
        reason = n.get("feedback_reason")
        if reason:
            lines.append(f"Why it failed: {reason}")
        subject, body = _entry_subject_body(n)
        if subject:
            lines.append(f"Subject: {subject}")
        lines.append("Body:")
        lines.append(_render_example_body(content_type, body))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

