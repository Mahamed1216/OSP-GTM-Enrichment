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


_BANNED_DIRECT_PITCH_PHRASES = [
    # Pain / timeline framing — banned from every direct pitch.
    "real tension",
    "weeks, not months",
    "weeks not months",
    "no ramp",
    "no attrition risk",
    # Legacy SDR-pitch phrases the v5 template replaces.
    "i could fill",
    "0 onboarding time",
    "fraction of the cost",
    "meetings and pipeline next week",
]


def detect_banned_direct_pitch_phrases(body: str) -> list[str]:
    """Flag the over-explanation phrases that leaked into SDR pitches.

    These were called out as too-AI-written in operator feedback. The
    template is two paragraphs; anything that adds "real tension" or
    "weeks not months" or the legacy "I could fill" / "fraction of the
    cost" wording indicates the model fell back to the verbose pattern.
    Returns the list of hits (empty == clean).
    """
    if not body:
        return []
    haystack = body.lower()
    return [p for p in _BANNED_DIRECT_PITCH_PHRASES if p in haystack]


# ---------------------------------------------------------------------------
# SDR direct-pitch coercer — deterministic rewrite when the model leaks the
# legacy pattern despite the prompt template.
# ---------------------------------------------------------------------------

_SDR_SIGNAL_PATTERNS = [
    re.compile(r"\bfounding\s+sdr\b", re.IGNORECASE),
    re.compile(r"\bfounding\s+bdr\b", re.IGNORECASE),
    re.compile(r"\bopen\s+sdr\b", re.IGNORECASE),
    re.compile(r"\bopen\s+bdr\b", re.IGNORECASE),
    re.compile(r"\bsdr\s+(?:req|role|hire|hiring)\b", re.IGNORECASE),
    re.compile(r"\bbdr\s+(?:req|role|hire|hiring)\b", re.IGNORECASE),
    re.compile(r"\bhiring\s+(?:a\s+|an\s+|the\s+|your\s+)?(?:founding\s+)?(?:s|b)dr\b", re.IGNORECASE),
    re.compile(r"\bsdr\s+req\b", re.IGNORECASE),
    re.compile(r"\bbdr\s+req\b", re.IGNORECASE),
    # Numbered counts: "5 SDRs", "10 BDRs", "fill the 5 SDRs you have open".
    re.compile(r"\b\d+\s+sdrs?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+bdrs?\b", re.IGNORECASE),
]


def _body_mentions_sdr_hiring(body: str) -> bool:
    """True iff the body talks about SDR/BDR-style hiring.

    Used as half of the coercion trigger: only rewrite when the body
    itself references SDR/BDR-hiring (so non-hiring emails are never
    touched). Combined with banned-phrase detection in the caller, this
    avoids destructive rewrites of correct outputs.
    """
    if not body:
        return False
    return any(p.search(body) for p in _SDR_SIGNAL_PATTERNS)


def _pick_role_phrase(body: str) -> str:
    """Choose the canonical role phrase from the body's own language.

    The prompt template restricts ROLE PHRASE to one of six fixed
    options. The order below is the matching priority: most specific
    wins (founding SDR / founding BDR), then numbered count, then
    generic single role. Defaults to "open SDR req" if SDR/BDR
    mentioned without enough specificity.
    """
    if not body:
        return "open SDR req"
    lower = body.lower()
    if "founding sdr" in lower:
        return "founding SDR"
    if "founding bdr" in lower:
        return "founding BDR"
    # Numbered counts: "5 SDRs", "10 SDR roles", "those 3 SDRs".
    count_match = re.search(
        r"\b(\d+)\s+(?:open\s+)?sdr(?:s|\s+roles?)?\b", body, re.IGNORECASE,
    )
    if count_match:
        n = int(count_match.group(1))
        if n >= 2:
            return f"those {n} SDRs"
    count_match = re.search(
        r"\b(\d+)\s+(?:open\s+)?bdr(?:s|\s+roles?)?\b", body, re.IGNORECASE,
    )
    if count_match:
        n = int(count_match.group(1))
        if n >= 2:
            return f"those {n} BDRs"
    if re.search(r"\bbdr\b", lower):
        return "open BDR req"
    return "open SDR req"


def canonical_sdr_direct_pitch(first_name: str, role_phrase: str) -> str:
    """Return the verbatim two-paragraph SDR direct pitch.

    The CTA is fixed ("Want to meet one of them?"). `role_phrase` is the
    only variable. Single-role variants take "your" ("your founding
    SDR", "your open SDR req"); the numbered-count variant doesn't
    ("those 5 SDRs"). No signature, no sender name — ends on the CTA.
    """
    fn = (first_name or "").strip() or "Hi"
    role = role_phrase.strip() or "open SDR req"
    # "those N SDRs/BDRs" already includes its own determiner; everything
    # else takes "your".
    opener_target = role if role.lower().startswith("those ") else f"your {role}"
    return (
        f"{fn},\n\n"
        f"Instead of hiring {opener_target}, I can basically guarantee you "
        "more results without the onboarding time.\n\n"
        "We have SDRs already trained selling to your buyers. All US based. "
        "Want to meet one of them?"
    )


# ---------------------------------------------------------------------------
# Structure 1 opening-hook stripper.
# ---------------------------------------------------------------------------
#
# Structure 1 emails (intro play) should start the body directly with the
# "Not sure if you're already working with..." question — no preamble hook
# sentence describing what the prospect's company does. Hooks like
#   "Mindsmith looks built for L&D teams running compliance programs."
# leak into B/C-tier sends and read as filler. The stripper removes any
# such sentence that lands BETWEEN the greeting and the Structure 1
# starter.

_STRUCTURE_1_STARTERS = [
    "not sure if you're already working with",
    "not sure if you're already partnering with",
    "curious if you're already getting in front of",
    "curious if you're already working with",
    "are you already working with",
    "are you already partnering with",
]

_STRUCTURE_2_MARKERS = [
    "instead of hiring",
]

# Hook-line shapes the user reported. Each pattern matches an ENTIRE
# line (after stripping leading/trailing whitespace), case-insensitive.
# The patterns are anchored on the verb phrase so a sentence about a
# company gets caught regardless of how many words the company name has.
_HOOK_LINE_PATTERNS = [
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+looks\s+built\s+for\b.*$", re.IGNORECASE),
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+is\s+built\s+for\b.*$", re.IGNORECASE),
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+feels\s+like\b.*$", re.IGNORECASE),
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+looks\s+like\b.*$", re.IGNORECASE),
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+is\s+a\s+wedge\b.*$", re.IGNORECASE),
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+is\s+a\s+natural\s+fit\b.*$", re.IGNORECASE),
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+is\s+a\s+clear\s+fit\b.*$", re.IGNORECASE),
    re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+\w+){0,3}\s+is\s+positioned\b.*$", re.IGNORECASE),
    re.compile(r"^Building\s+infrastructure\s+for\b.*$", re.IGNORECASE),
]


def _line_is_structure_1_starter(line: str) -> bool:
    lower = line.strip().lower()
    return any(s in lower for s in _STRUCTURE_1_STARTERS)


def _line_is_opening_hook(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _line_is_structure_1_starter(stripped):
        return False
    return any(p.match(stripped) for p in _HOOK_LINE_PATTERNS)


def strip_structure_1_opening_hook(body: str) -> tuple[str, str | None]:
    """Remove a preamble hook line between the greeting and the
    Structure 1 starter, if both are present.

    Triggers only when:
      - The body contains a Structure 1 starter
        ("not sure if you're already working with ..." and variants).
      - The body does NOT contain a Structure 2 marker
        ("instead of hiring ..."). Structure 2 direct pitches are left
        completely untouched.

    Walks from the greeting forward, drops every line that matches one
    of the hook patterns until it hits the Structure 1 starter (or a
    non-hook line, which stops the walk to avoid accidentally eating
    real body content). Blank lines around removed hooks are also
    cleaned so the rendered output doesn't have orphan whitespace.

    Returns (new_body, warning_or_None).
    """
    if not body:
        return body, None
    lower = body.lower()
    if not any(s in lower for s in _STRUCTURE_1_STARTERS):
        return body, None
    if any(m in lower for m in _STRUCTURE_2_MARKERS):
        return body, None

    lines = body.split("\n")
    # Find the first non-empty line — the greeting.
    greeting_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip():
            greeting_idx = i
            break
    if greeting_idx is None:
        return body, None

    removed_any = False
    # Walk forward from greeting+1. Drop matched hook lines AND the
    # blank lines that lead into them, until we either hit a non-hook
    # line or the Structure 1 starter.
    i = greeting_idx + 1
    while i < len(lines):
        # Skip blank lines but remember where we started so we can also
        # drop them if the next non-blank line turns out to be a hook.
        blank_start = i
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        line = lines[i]
        if _line_is_structure_1_starter(line):
            # Reached the starter — leave it untouched.
            break
        if _line_is_opening_hook(line):
            # Mark the blank prefix + the hook line for removal.
            del lines[blank_start:i + 1]
            i = blank_start
            removed_any = True
            continue
        # First real content line that ISN'T a hook — stop walking.
        # (Don't strip lines we don't recognize; that's safer than
        # accidentally eating a sentence the writer meant to keep.)
        break

    if not removed_any:
        return body, None

    # Re-join. Collapse runs of 3+ blank lines created by removals.
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, "Removed Structure 1 opening hook."


def coerce_structure_1_named_buyers(
    body: str,
    *,
    lead_company: str,
    buyer_accounts: list[str],
) -> tuple[str, str | None]:
    """Rewrite a Structure 1 intro/CTA to use the 2 named buyer accounts.

    Triggers when:
      - The body has a Structure 1 starter ("Not sure if you're already
        working with ...") AND no Structure 2 marker ("Instead of
        hiring ...").
      - `buyer_accounts` has at least 2 names.
      - The body doesn't already include BOTH of the first two buyer
        names (case-insensitive substring match).

    Action — replace everything from the Structure 1 starter line to
    end-of-body with a canonical two-paragraph block:

        Not sure if you're already working with <A> or <B>? They seem
        like a great fit for what <Lead Company> does.

        Happy to show you how we could make an intro to them or
        companies like them. Just let me know.

    Returns (body, warning_or_None). Greeting (and any blank line after
    it) is preserved untouched.
    """
    if not body or len(buyer_accounts) < 2:
        return body, None
    a = (buyer_accounts[0] or "").strip()
    b = (buyer_accounts[1] or "").strip()
    if not a or not b:
        return body, None

    lower = body.lower()
    if not any(s in lower for s in _STRUCTURE_1_STARTERS):
        return body, None
    if any(m in lower for m in _STRUCTURE_2_MARKERS):
        return body, None

    # Already correct: both names present in body.
    a_in_body = a.lower() in lower
    b_in_body = b.lower() in lower
    if a_in_body and b_in_body:
        return body, None

    company = (lead_company or "").strip() or "the team"
    new_intro = (
        f"Not sure if you're already working with {a} or {b}? "
        f"They seem like a great fit for what {company} does."
    )
    new_cta = (
        "Happy to show you how we could make an intro to them or "
        "companies like them. Just let me know."
    )

    lines = body.split("\n")
    greeting_idx = 0
    for i, line in enumerate(lines):
        if line.strip():
            greeting_idx = i
            break
    starter_idx: int | None = None
    for i in range(greeting_idx + 1, len(lines)):
        if any(s in lines[i].lower() for s in _STRUCTURE_1_STARTERS):
            starter_idx = i
            break
    if starter_idx is None:
        return body, None

    # Keep [greeting + any blanks up to starter] as the preamble; drop
    # from starter through end; substitute canonical intro + CTA.
    preamble = lines[:starter_idx]
    # Trim trailing blanks on preamble so we don't stack 3+ newlines.
    while preamble and not preamble[-1].strip():
        preamble.pop()
    new_lines = preamble + ["", new_intro, "", new_cta]
    return "\n".join(new_lines), (
        f"Rewrote Structure 1 intro to use named buyers ({a}, {b})."
    )


def coerce_sdr_direct_pitch(
    body: str, *, first_name: str,
) -> tuple[str, str | None]:
    """Deterministically rewrite an SDR-hiring body to the canonical pattern.

    Trigger: body mentions SDR/BDR-hiring AND
      (a) body contains any banned legacy phrase
       OR (b) body does not already follow the canonical opener
              ("Instead of hiring") + closing CTA ("Want to meet one of them?").

    When triggered, replace the body with the canonical two-paragraph
    template using a role phrase picked from the original body. Returns
    `(new_body, warning_or_None)` — `warning_or_None` is a short note
    suitable for stashing in `signals_cited` so the operator sees what
    happened.

    When not triggered, returns the body unchanged with `None`.
    """
    if not body:
        return body, None
    if not _body_mentions_sdr_hiring(body):
        return body, None

    has_banned = bool(detect_banned_direct_pitch_phrases(body))
    has_canonical_opener = bool(
        re.search(r"\binstead of hiring\b", body, re.IGNORECASE)
    )
    has_canonical_cta = bool(
        re.search(
            r"\bwant to meet (?:one of them|one of em|them)\b\s*\??",
            body,
            re.IGNORECASE,
        )
    )
    if has_canonical_opener and has_canonical_cta and not has_banned:
        # Already on template — leave it alone.
        return body, None

    role_phrase = _pick_role_phrase(body)
    rewritten = canonical_sdr_direct_pitch(first_name, role_phrase)
    return rewritten, (
        f"Rewrote SDR-hiring body to canonical Eric pattern "
        f"(role_phrase={role_phrase!r})."
    )


def detect_partner_channel_mismatch(
    body: str,
    *,
    buyer_motion: str | None = None,
    likely_direct_buyers: list[str] | None = None,
    likely_partner_channels: list[str] | None = None,
) -> list[str]:
    """Flag B2C / partner-channel framing mistakes.

    Two cases:
      1. `buyer_motion` is B2C / B2B2C / partner_led / marketplace AND
         likely_direct_buyers is empty, but the body uses buyer language
         ("great fit for what X does", "working with"). Should have used
         partner-channel framing instead.
      2. Body explicitly references partner_channels (e.g. "partnering
         with X, Y, or Z") but the CTA says "companies like them" —
         partner framing requires "teams like that" / "channels like
         that" CTA.

    Heuristic, not a hard block. Returns warning strings the caller
    attaches to signals_cited so the operator can review.
    """
    if not body:
        return []
    warnings: list[str] = []
    body_lower = body.lower()
    direct = list(likely_direct_buyers or [])
    partners = list(likely_partner_channels or [])

    is_partner_motion = (buyer_motion or "").lower() in (
        "b2c", "b2b2c", "partner_led", "marketplace",
    )

    # Case 1: B2C-style motion but body talks about "working with" / "great fit"
    uses_buyer_language = bool(
        re.search(r"\bworking\s+with\b", body_lower)
        or re.search(r"\bgreat\s+fit\s+for\s+what\b", body_lower)
    )
    uses_partner_language = bool(
        re.search(r"\bpartner(?:ing)?\s+with\b", body_lower)
        or re.search(r"\b(?:stronger|strong)\s+channel\s+for\s+what\b", body_lower)
        or re.search(r"\bdistribution\s+channel\b", body_lower)
    )
    if is_partner_motion and not direct and uses_buyer_language and not uses_partner_language:
        warnings.append(
            f"buyer_motion={buyer_motion!r} but body uses buyer language "
            "('working with' / 'great fit for what X does'). Switch to "
            "partner-channel framing: 'partnering with ...' + 'stronger "
            "channel for what X does'."
        )

    # Case 2: Partner segments named in the body but CTA uses
    # "companies like them" — partner framing requires "teams like that"
    # or "channels like that".
    has_companies_like = bool(
        re.search(r"\bcompanies\s+like\s+(?:them|that)\b", body_lower)
    )
    if (
        has_companies_like
        and uses_partner_language
        and not any(
            (b.lower() in body_lower)
            for b in direct
            if b and b.strip()
        )
    ):
        warnings.append(
            "Body uses partner-channel framing but CTA says 'companies "
            "like them'. Partner framing requires 'teams like that' or "
            "'channels like that'."
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
