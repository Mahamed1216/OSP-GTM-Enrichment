"""Prompt-overlay cleanup: dedupe sections, merge addendums safely.

Why this module exists:
  - A prior approval flow let the self-improvement loop append addendums
    directly to the overlay text. Repeated saves through the sectioned
    editor + repeated addendum appends accumulated duplicate H1 headers,
    most visibly `# EXAMPLE — MATCH THIS VOICE EXACTLY` repeating 10
    times in the live overlay.
  - The editor's section parser is naive: it splits on `^# ` and keeps
    every section in order — including duplicates — so duplicates
    persisted across saves.

This module is the safety layer. `save_overlay` (in loader.py) calls
`dedupe_email_sections` before any DB write, so the live prompt cannot
grow duplicate H1 headers no matter how many times an addendum is
applied.

`merge_self_improvement_addendum` is the only sanctioned path the
self-improvement loop uses to apply approved changes — it writes into
a SINGLE `# SELF IMPROVEMENT ADDENDUM` section and merges additional
approvals into the same section instead of stacking new sections.
"""
from __future__ import annotations

import re

# The 12 sections the default email prompt defines. The cleanup walks
# these in this canonical order; any unknown section header is kept but
# placed AFTER the known ones in its original relative order.
KNOWN_EMAIL_SECTIONS: list[str] = [
    "SENDER",
    "ROLE",
    "MESSAGE STANCE — MOST IMPORTANT RULE",
    "VOICE — PEER-TO-PEER TEXT",
    "HOOK — NAME SOMETHING CONCRETE",
    "GOAL OF THIS MESSAGE",
    "BUYER ACCOUNTS — WHO TO NAME",
    "BANNED — DO NOT USE",
    "STRUCTURE — STRICT",
    "PUNCTUATION",
    "CTA — VARY EVERY EMAIL",
    "OUTPUT FORMAT",
    "EXAMPLE — MATCH THIS VOICE EXACTLY",
    "SELF IMPROVEMENT ADDENDUM",
]

# The header self-improvement addendums merge into. One section, always.
SELF_IMPROVEMENT_HEADER = "SELF IMPROVEMENT ADDENDUM"


def _normalize_header(header: str) -> str:
    """Header equality is whitespace-insensitive and dash-tolerant.

    The default prompt uses em-dashes in section headers ("MESSAGE
    STANCE — MOST IMPORTANT RULE"); a save through certain text editors
    converts those to ASCII hyphens. Dedupe must treat the two as the
    same section, otherwise the editor's "Save" round-trip would itself
    create duplicates.
    """
    h = header.strip()
    h = h.replace("–", "-").replace("—", "-")
    h = re.sub(r"\s+", " ", h)
    return h.upper()


def _split_sections(prompt: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (preamble, [(header, body), ...]).

    Preamble is text before the first `^# ` header (usually empty for
    the email prompt). Sections are returned in the order they appear.
    """
    if not prompt or not prompt.strip():
        return "", []
    parts = re.split(r"^# ", prompt, flags=re.MULTILINE)
    preamble = parts[0].rstrip()
    sections: list[tuple[str, str]] = []
    for part in parts[1:]:
        lines = part.split("\n", 1)
        header = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        # Strip trailing whitespace but preserve internal blank lines —
        # examples in the default prompt rely on them.
        sections.append((header, body.rstrip()))
    return preamble, sections


def _join_sections(preamble: str, sections: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    if preamble.strip():
        parts.append(preamble.strip())
    for header, body in sections:
        body_clean = body.rstrip()
        if body_clean:
            parts.append(f"# {header}\n{body_clean}")
        else:
            parts.append(f"# {header}")
    return "\n\n".join(parts).rstrip() + "\n"


def dedupe_email_sections(prompt: str) -> tuple[str, dict[str, int]]:
    """Collapse duplicate H1 sections to a single occurrence each.

    Strategy:
      - Walk sections in order. The FIRST occurrence of each
        normalized header is kept. Subsequent occurrences are dropped.
      - The cleanup is order-preserving: section ordering follows the
        first-occurrence ordering in the input.

    Returns (cleaned_prompt, stats) where stats keys are the original
    section headers and values are how many duplicates were removed.
    """
    preamble, sections = _split_sections(prompt)
    seen: dict[str, int] = {}
    kept: list[tuple[str, str]] = []
    duplicates: dict[str, int] = {}
    for header, body in sections:
        key = _normalize_header(header)
        if key in seen:
            duplicates[header] = duplicates.get(header, 0) + 1
            continue
        seen[key] = len(kept)
        kept.append((header, body))
    return _join_sections(preamble, kept), duplicates


def merge_self_improvement_addendum(
    prompt: str, new_directive: str
) -> str:
    """Append an addendum into a SINGLE `# SELF IMPROVEMENT ADDENDUM` section.

    If the section already exists, the new directive is appended to its
    body (separated by a blank line). If it doesn't exist, it's created
    at the end of the prompt. This is the ONLY sanctioned path the
    self-improvement loop uses, so duplicate top-level headers can never
    accumulate from approvals.
    """
    new_directive = (new_directive or "").strip()
    if not new_directive:
        return prompt

    preamble, sections = _split_sections(prompt)
    target_key = _normalize_header(SELF_IMPROVEMENT_HEADER)
    for i, (header, body) in enumerate(sections):
        if _normalize_header(header) == target_key:
            new_body = (body.rstrip() + "\n\n" + new_directive).strip()
            sections[i] = (header, new_body)
            # Dedupe in case prior writes accidentally created two
            # SELF IMPROVEMENT ADDENDUM sections.
            joined = _join_sections(preamble, sections)
            deduped, _ = dedupe_email_sections(joined)
            return deduped

    sections.append((SELF_IMPROVEMENT_HEADER, new_directive))
    return _join_sections(preamble, sections)


def section_summary(prompt: str) -> list[tuple[str, int]]:
    """Return [(header, occurrence_count), ...] — used by the editor's
    "show me what's in this prompt" debug panel."""
    _, sections = _split_sections(prompt)
    counts: dict[str, int] = {}
    order: list[str] = []
    for header, _ in sections:
        if header not in counts:
            order.append(header)
        counts[header] = counts.get(header, 0) + 1
    return [(h, counts[h]) for h in order]
