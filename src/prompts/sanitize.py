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


def detect_segment_vs_company_mismatch(
    body: str,
    *,
    named_buyer_accounts: list[str] | None = None,
) -> list[str]:
    """Flag the segments-but-CTA-says-"companies like them" mismatch.

    Heuristic, not a hard block — returns a list of warning strings the
    caller (email writer) attaches to signals_cited / metadata for the
    operator to review. Returns [] when no mismatch is detected.

    Logic:
      - Look at the first ~2 sentences of the body to find a list-style
        intro question ("are you working with X, Y, or Z?", "not sure
        if you're working with X, Y").
      - If that list contains ONLY tokens that look like segments
        (multi-word noun phrases, plurals, no Titlecase brand markers)
        and the body later contains the literal phrase "companies like
        them" / "companies like that", flag the mismatch.
      - If `named_buyer_accounts` is provided and any of those names
        appear in the body, the mismatch is auto-cleared.
    """
    if not body:
        return []
    warnings: list[str] = []

    has_companies_like = bool(
        re.search(r"\bcompanies\s+like\s+(?:them|that)\b", body, flags=re.IGNORECASE)
    )
    if not has_companies_like:
        return warnings

    if named_buyer_accounts:
        haystack = body.lower()
        if any(
            name.lower() in haystack
            for name in named_buyer_accounts
            if name and name.strip()
        ):
            # Body actually names at least one buyer account, so the
            # "companies like them" CTA has a referent.
            return warnings

    # Find the first intro question that lists candidates.
    intro_re = re.compile(
        r"(?:are you|not sure if you're|curious if you're)[^?]*?with\s+([^?\n]+)\?",
        flags=re.IGNORECASE,
    )
    match = intro_re.search(body)
    if not match:
        # No structured intro list; can't reason about it. Flag softly.
        warnings.append(
            "Body uses 'companies like them' but no buyer-account list "
            "was detected — verify the CTA referent."
        )
        return warnings

    candidate_list = match.group(1)
    items = re.split(r",|\bor\b", candidate_list)
    items = [i.strip() for i in items if i.strip()]
    if not items:
        return warnings

    # A "named company" looks like a Titlecase brand token (e.g. JPMorgan,
    # UnitedHealth) or has a domain suffix. A "segment" reads as a multi-
    # word lowercase noun phrase ("financial services firms", "healthcare
    # systems"). The classifier is intentionally conservative: if ANY
    # item looks like a brand, we assume real companies were named.
    looks_like_brand = re.compile(
        r"^(?:[A-Z][A-Za-z0-9&\-]*(?:\s+[A-Z][A-Za-z0-9&\-]*){0,3})$"
    )
    any_brand = any(looks_like_brand.match(i) for i in items)
    if not any_brand:
        warnings.append(
            "Body lists buyer SEGMENTS but the CTA says 'companies like "
            "them' — switch CTA to 'teams like that' or name actual buyer "
            "companies. Intro list: " + ", ".join(items)
        )
    return warnings


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
