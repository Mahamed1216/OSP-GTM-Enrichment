"""Split a prompt overlay into editable sections and put it back together.

The prompt bodies are markdown with `# TITLE` headers, so the console can edit
one section at a time and recombine them on save. Round-tripping is exact:
``compile_sections(split_sections(text)) == text`` for any prompt that starts
with a header.

Sections are read from the prompt itself rather than a fixed list — a hardcoded
list would silently drop a section the prompt has (e.g. BUYER ACCOUNTS) or
invent empty ones it doesn't. SUGGESTED_SECTIONS is only offered in the editor
as titles an operator may choose to add.
"""
from __future__ import annotations

import re

_HEADER = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)

# Titles the console offers under "add a section". Not enforced, not required —
# a prompt is valid with any subset, in any order.
SUGGESTED_SECTIONS: tuple[str, ...] = (
    "SENDER",
    "ROLE",
    "MESSAGE STANCE — MOST IMPORTANT RULE",
    "VOICE — PEER-TO-PEER TEXT",
    "CONTEXT ROUTING — MATCH THE MESSAGE TO THE SIGNAL",
    "HOOK — NAME SOMETHING CONCRETE",
    "OPERATIONAL HYPOTHESIS — MAKE THE PROBLEM FEEL REAL",
    "REVENUE ANGLE — DO NOT DEFAULT TO COST SAVINGS",
    "BUYER ACCOUNTS — WHO TO NAME",
    "GOAL OF THIS MESSAGE",
    "BANNED — DO NOT USE",
    "STRUCTURE — STRICT",
    "PUNCTUATION",
    "CTA — VARY EVERY EMAIL",
    "OUTPUT FORMAT",
    "EXAMPLE — MATCH THIS VOICE EXACTLY",
)


def split_sections(text: str) -> list[dict[str, str]]:
    """Return ``[{"title": ..., "body": ...}]`` in document order.

    Any text before the first header is returned under the empty title so it
    survives a round trip instead of being silently dropped.
    """
    text = text or ""
    matches = list(_HEADER.finditer(text))
    if not matches:
        return [{"title": "", "body": text}] if text else []

    sections: list[dict[str, str]] = []
    preamble = text[: matches[0].start()]
    if preamble.strip():
        sections.append({"title": "", "body": preamble.rstrip("\n")})

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end]
        sections.append({"title": match.group(1), "body": body.strip("\n")})
    return sections


def compile_sections(sections: list[dict[str, str]]) -> str:
    """Rebuild the full prompt from sections, preserving order."""
    parts: list[str] = []
    for section in sections:
        title = (section.get("title") or "").strip()
        body = (section.get("body") or "").strip("\n")
        parts.append(f"# {title}\n{body}" if title else body)
    return "\n\n".join(part for part in parts if part.strip()) + "\n"
