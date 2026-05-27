"""Belt-and-suspenders cleanup of LLM output before persistence.

The v4 prompts forbid em-dashes, [Sender] placeholders, and dash-prefixed
sign-offs, but model leakage happens. This module is the safety net.
Applied to subject + body of every generated artifact before the
GeneratedContent row is committed.

Beyond the v4 rules, the v5-era rewrite enforces:
  - NO sender-name sign-off at the end of the body. The prompt was
    rewritten to drop the "sign off with sender's first name" rule and
    the four examples were updated to end on the CTA, but the model
    still leaks signatures occasionally. `_strip_trailing_signature`
    removes them at persistence time so the operator sees the same
    clean output regardless.
  - A best-effort flag for "competitor named as buyer" (returned by
    `validate_generated_email`, used by the bulk-regen UI to surface
    suspicious emails for review rather than reject silently).
"""
from __future__ import annotations

import re
from typing import Iterable

_PLACEHOLDERS = [r"\[Sender\]", r"\[Your Name\]", r"\[Name\]", r"\[FirstName\]"]
_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDERS), flags=re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r" {2,}")

# Common closings that sometimes survive the prompt's "no sign-off" rule.
_TRAILING_CLOSING_LINES = {
    "best", "best regards", "best,", "regards", "regards,",
    "thanks", "thanks!", "thanks,", "thank you", "thank you,",
    "cheers", "cheers,", "sincerely", "sincerely,",
    "talk soon", "looking forward", "looking forward!",
}

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


def _strip_trailing_signature(text: str, sender_first_name: str) -> str:
    """Remove a trailing sender-name / closing line, if present.

    Walks the body from the end, dropping any line that is (a) the
    sender's first name on its own, (b) a known closing word
    ("thanks", "best", etc.), or (c) a dash-prefixed variant of the
    above. Stops as soon as it hits a line that isn't a closing — so
    the CTA above the signature is preserved untouched.

    Idempotent. Safe to run on emails that already end on the CTA
    (no-op).
    """
    if not text or not sender_first_name:
        return text
    lines = text.splitlines()
    # Strip trailing blank lines first so the index math is simple.
    while lines and not lines[-1].strip():
        lines.pop()
    sender_lower = sender_first_name.strip().lower()
    dash_prefix_re = re.compile(rf"^[\-—–]\s*{re.escape(sender_first_name)}\s*$", re.IGNORECASE)
    changed = False
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            changed = True
            continue
        if last.lower().rstrip(",.!") == sender_lower:
            lines.pop()
            changed = True
            continue
        if dash_prefix_re.match(last):
            lines.pop()
            changed = True
            continue
        if last.lower().rstrip(",.!") in _TRAILING_CLOSING_LINES:
            lines.pop()
            changed = True
            continue
        # Hit a non-closing line — the CTA. Stop.
        break
    if not changed:
        return text
    # Trim any blank tail that the strip created.
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


_VOICE_AI_COMPETITORS = {
    "bland ai", "bland.ai", "retell ai", "retell.ai", "elevenlabs",
    "vapi", "vapi.ai", "deepgram", "playht", "play.ht", "play ht",
}


def detect_competitor_as_buyer(
    body: str,
    *,
    company_industry: str | None = None,
    competitor_seed: Iterable[str] | None = None,
) -> list[str]:
    """Best-effort flag: return any competitor names that appear in the
    body as candidate "buyers".

    The heuristic is intentionally narrow — it only fires on names from
    a curated competitor list (currently voice AI as the user's
    Ultravox.ai case) plus any seed provided by the caller. False
    negatives are expected; the goal is to flag the most common slip
    (a competitor list pasted into an intro play) for human review.
    """
    if not body:
        return []
    haystack = body.lower()
    seed = {s.strip().lower() for s in (competitor_seed or []) if s and s.strip()}
    industry = (company_industry or "").lower()
    candidates = set(seed)
    if "voice" in industry and ("ai" in industry or "agent" in industry):
        candidates |= _VOICE_AI_COMPETITORS
    flagged: list[str] = []
    for name in candidates:
        # Word-boundary match so "bland ai" doesn't fire on "bland-ai".
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        if pattern.search(haystack):
            flagged.append(name)
    return sorted(set(flagged))


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

    # Final pass: strip any trailing sender-name / closing line. Runs
    # LAST so prior substitutions (em-dash normalization, etc.) can't
    # re-introduce a signature line.
    text = _strip_trailing_signature(text, sender_first_name)
    return text
