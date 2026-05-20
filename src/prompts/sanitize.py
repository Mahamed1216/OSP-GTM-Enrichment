"""Belt-and-suspenders cleanup of LLM output before persistence.

The v4 prompts forbid em-dashes, [Sender] placeholders, and dash-prefixed
sign-offs, but model leakage happens. This module is the safety net.
Applied to subject + body of every generated artifact before the
GeneratedContent row is committed.
"""
from __future__ import annotations

import re

_PLACEHOLDERS = [r"\[Sender\]", r"\[Your Name\]", r"\[Name\]", r"\[FirstName\]"]
_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDERS), flags=re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r" {2,}")

# Em-dash → sentence boundary. Capitalize the following letter so the
# substitution reads as a clean sentence break, not a typo. We only eat
# spaces/tabs around the dash (not newlines) so line-break sign-offs like
# "Best,\n— Mohammed" stay on separate lines when line_prefix_re misses.
# Leading "." is optionally consumed to avoid producing ".." in cases like
# "end of sentence. — Next".
_EM_DASH_SUB_RE = re.compile(r"\.?[ \t]*—[ \t]*([A-Za-z])?")


def _em_dash_to_sentence_boundary(m: re.Match[str]) -> str:
    nxt = m.group(1)
    return f". {nxt.upper()}" if nxt else ". "


def sanitize_generated_text(text: str, sender_first_name: str) -> str:
    if not text:
        return text

    text = _PLACEHOLDER_RE.sub(sender_first_name, text)

    line_prefix_re = re.compile(
        rf"(^|\n)[—–\-]\s+{re.escape(sender_first_name)}\b"
    )
    text = line_prefix_re.sub(rf"\1{sender_first_name}", text)

    text = _EM_DASH_SUB_RE.sub(_em_dash_to_sentence_boundary, text)
    text = text.replace(". .", ".")
    text = text.replace(" – ", " - ").replace("–", "-")

    text = _MULTI_SPACE_RE.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text
