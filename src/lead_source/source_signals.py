"""Parse imported enrichment / signals from an OSP Lead Engine ContactOut.

The colleague's sourcing system is signal-first: a contact is only captured
when a signal fires, so the payload often carries the original signal(s),
matched ICPs, a source tier/score, and source metadata.

This module is PURE (no DB, no I/O) so it's easy to unit-test against real
and degenerate payloads. Persistence lives in src.signals.store.

Normalized per-signal shape:
  signal_type, signal_name, signal_strength (none|low|medium|high),
  signal_summary, signal_evidence, signal_source_url, signal_detected_at,
  raw_signal
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Keys we probe for each normalized field — payloads vary, so try several.
_TYPE_KEYS = ("signal_type", "type", "category", "kind")
_NAME_KEYS = ("signal_name", "name", "title", "label", "signal", "headline")
_STRENGTH_KEYS = ("signal_strength", "strength", "confidence", "severity", "level")
_SCORE_KEYS = ("score", "confidence_score", "signal_score", "weight")
_SUMMARY_KEYS = ("signal_summary", "summary", "description", "detail", "text", "message")
_EVIDENCE_KEYS = ("signal_evidence", "evidence", "details", "snippet", "context", "proof")
_URL_KEYS = ("signal_source_url", "source_url", "url", "link", "href")
_DETECTED_KEYS = ("signal_detected_at", "detected_at", "created_at", "timestamp", "date", "observed_at")

_STRONG = {"high", "strong", "verified", "confirmed"}
_MEDIUM = {"medium", "moderate", "mid", "likely"}
_WEAK = {"low", "weak", "possible", "unknown", "none"}


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _normalize_strength(raw_strength: Any, raw_score: Any) -> str:
    """Map a free-form strength label and/or numeric score to none|low|medium|high."""
    if isinstance(raw_strength, str):
        s = raw_strength.strip().lower()
        if s in _STRONG:
            return "high"
        if s in _MEDIUM:
            return "medium"
        if s in _WEAK:
            return "low" if s != "none" else "none"
    # Numeric score fallback. Accept 0-1 floats or 0-100 ints.
    val = raw_score if raw_score is not None else raw_strength
    try:
        num = float(val)
    except (TypeError, ValueError):
        return "low"  # unstructured / unknown → stored, never bumps
    if num <= 1.0:
        num *= 100.0
    if num >= 70:
        return "high"
    if num >= 40:
        return "medium"
    return "low"


@dataclass
class NormalizedSignal:
    signal_type: str | None = None
    signal_name: str | None = None
    signal_strength: str = "low"
    signal_summary: str | None = None
    signal_evidence: str | None = None
    signal_source_url: str | None = None
    signal_detected_at: str | None = None
    raw_signal: Any = None


def _normalize_one(sig: Any) -> NormalizedSignal:
    if isinstance(sig, dict):
        strength = _normalize_strength(_first(sig, _STRENGTH_KEYS), _first(sig, _SCORE_KEYS))
        name = _first(sig, _NAME_KEYS) or _first(sig, _TYPE_KEYS)
        return NormalizedSignal(
            signal_type=_first(sig, _TYPE_KEYS),
            signal_name=str(name) if name is not None else None,
            signal_strength=strength,
            signal_summary=_opt_str(_first(sig, _SUMMARY_KEYS)),
            signal_evidence=_opt_str(_first(sig, _EVIDENCE_KEYS)),
            signal_source_url=_opt_str(_first(sig, _URL_KEYS)),
            signal_detected_at=_opt_str(_first(sig, _DETECTED_KEYS)),
            raw_signal=sig,
        )
    # Unstructured (a bare string / number) — keep it, but it never bumps.
    text = str(sig).strip()
    return NormalizedSignal(
        signal_name=text or None,
        signal_strength="low",
        signal_summary=text or None,
        raw_signal=sig,
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


_STRENGTH_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class ParsedSourceSignals:
    has_signals: bool = False
    signals: list[dict] = field(default_factory=list)        # normalized
    raw_signals: Any = None                                  # original payload.signals
    matched_icps: list[str] = field(default_factory=list)
    source_system: str | None = None
    source_metadata: dict = field(default_factory=dict)
    source_tier: str | None = None
    source_tier_score: float | None = None
    enrichment_status: str | None = None
    email_verified: bool = False
    email_bounce_risk: str | None = None
    email_source: str | None = None
    mobile_verified: bool | None = None
    mobile_verification_status: str | None = None
    overall_strength: str = "none"
    strong_signal_count: int = 0
    strong_signal_names: list[str] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str_list(value: Any) -> list[str]:
    """Normalize matched_icps which may be list[str] or list[dict]."""
    if not value:
        return []
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                slug = item.get("slug") or item.get("name") or item.get("icp")
                if slug:
                    out.append(str(slug))
            elif item:
                out.append(str(item))
    elif isinstance(value, str):
        out.append(value)
    return out


def parse_source_signals(contact: dict[str, Any]) -> ParsedSourceSignals:
    """Extract + normalize source signals/enrichment from a ContactOut dict.

    Never raises — degenerate payloads yield an empty result.
    """
    contact = contact or {}
    raw_signals = contact.get("signals")
    normalized: list[NormalizedSignal] = []
    if isinstance(raw_signals, list):
        normalized = [_normalize_one(s) for s in raw_signals if s not in (None, "")]
    elif isinstance(raw_signals, dict) and raw_signals:
        normalized = [_normalize_one(raw_signals)]
    elif raw_signals:  # a bare string
        normalized = [_normalize_one(raw_signals)]

    strong = [
        n for n in normalized
        if _STRENGTH_RANK.get(n.signal_strength, 0) >= _STRENGTH_RANK["medium"]
    ]
    overall = "none"
    for n in normalized:
        if _STRENGTH_RANK.get(n.signal_strength, 0) > _STRENGTH_RANK.get(overall, 0):
            overall = n.signal_strength

    matched_icps = _coerce_str_list(contact.get("matched_icps") or contact.get("icp"))
    source_tier = _opt_str(contact.get("tier"))
    source_tier_score = _coerce_float(contact.get("tier_score"))

    parsed = ParsedSourceSignals(
        has_signals=bool(normalized),
        signals=[asdict(n) for n in normalized],
        raw_signals=raw_signals,
        matched_icps=matched_icps,
        source_system=_opt_str(contact.get("source")),
        source_metadata=contact.get("source_metadata") if isinstance(contact.get("source_metadata"), dict) else {},
        source_tier=source_tier,
        source_tier_score=source_tier_score,
        enrichment_status=_opt_str(contact.get("enrichment_status")),
        email_verified=bool(contact.get("email_verified", False)),
        email_bounce_risk=_opt_str(contact.get("email_bounce_risk")),
        email_source=_opt_str(contact.get("email_source")),
        mobile_verified=contact.get("mobile_verified"),
        mobile_verification_status=_opt_str(contact.get("mobile_verification_status")),
        overall_strength=overall,
        strong_signal_count=len(strong),
        strong_signal_names=[n.signal_name for n in strong if n.signal_name],
    )
    parsed.summary = build_summary(parsed)
    return parsed


def build_summary(parsed: ParsedSourceSignals) -> str:
    """Short human-readable summary of the imported signals."""
    if not parsed.has_signals:
        return ""
    names = [
        (s.get("signal_name") or s.get("signal_type") or "signal")
        for s in parsed.signals
    ]
    bits: list[str] = []
    bits.append(
        f"{len(parsed.signals)} source signal(s) "
        f"({parsed.overall_strength} strength): " + ", ".join(names[:5])
    )
    if parsed.matched_icps:
        bits.append("matched ICPs: " + ", ".join(parsed.matched_icps[:4]))
    if parsed.source_tier:
        score_bit = (
            f" ({parsed.source_tier_score})" if parsed.source_tier_score is not None else ""
        )
        bits.append(f"source tier {parsed.source_tier}{score_bit}")
    return "; ".join(bits)
