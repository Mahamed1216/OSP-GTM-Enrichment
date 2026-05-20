"""Live Anthropic API contract test for src.llm.generate_json.

Catches regressions when Anthropic deprecates parameters (e.g. `temperature`
on Opus 4.7) by exercising the real API against both the scoring and content
models we use in production.

Skipped by default. Run locally with:

    RUN_LIVE_API_TESTS=1 ANTHROPIC_API_KEY=sk-ant-... pytest tests/test_llm_live.py
"""
from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from src.config import settings
from src.llm import generate_json

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_API_TESTS") != "1",
    reason="Live API test gated by RUN_LIVE_API_TESTS=1",
)


class _PingResult(BaseModel):
    answer: int


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_label",
    ["scoring", "content"],
)
async def test_generate_json_accepts_our_kwargs(model_label: str):
    """Both production models must accept whatever generate_json passes today.

    We use the smallest possible request to keep this test cheap. If Anthropic
    deprecates another kwarg in the future, this fails with 400 and we know.
    """
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")

    model = (
        settings.scoring_model if model_label == "scoring" else settings.content_model
    )

    result = await generate_json(
        model=model,
        system='You answer arithmetic. Reply with JSON only: {"answer": <int>}.',
        user="What is 2 + 2?",
        schema=_PingResult,
        max_tokens=50,
    )
    assert result.answer == 4
