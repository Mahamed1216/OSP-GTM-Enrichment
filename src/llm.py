"""Anthropic SDK helper: cached system prompt + JSON-schema enforced output."""
import json
import logging
import re
from typing import Type, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from src.config import settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _extract_first_json_block(text: str) -> str:
    """Find the first {...} or [...] block by brace-counting (handles strings)."""
    text = _strip_fences(text)
    if not text:
        return text
    if text[0] not in "{[":
        idx = min(
            (text.find(c) for c in "{[" if text.find(c) != -1),
            default=-1,
        )
        if idx < 0:
            return text
        text = text[idx:]
    open_ch, close_ch = (text[0], "}" if text[0] == "{" else "]")
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[: i + 1]
    return text


async def generate_json(
    *,
    model: str,
    system: str,
    user: str,
    schema: Type[T],
    max_tokens: int = 1500,
) -> T:
    """Call Claude with a cached system prompt and parse JSON output into `schema`."""
    response = await client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    )
    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    cleaned = _extract_first_json_block(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.error("llm_json_parse_failed", extra={"raw": raw_text[:500]})
        raise ValueError(f"LLM returned invalid JSON: {exc}") from exc
    try:
        return schema.model_validate(parsed)
    except ValidationError as exc:
        log.error("llm_schema_validation_failed", extra={"errors": exc.errors(), "raw": raw_text[:500]})
        raise
