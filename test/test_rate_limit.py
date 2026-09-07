"""Global LLM/VLM request-limit regression tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from wiki_agent.config import LLMConfig, VLMConfig
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.llm.rate_limit import RequestLimiter


def test_async_concurrency_is_capped() -> None:
    limiter = RequestLimiter(
        max_concurrency=2,
        requests_per_minute=0,
        tokens_per_minute=0,
    )
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with limiter.async_slot(1):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    async def run_workers() -> None:
        await asyncio.gather(*[worker() for _ in range(6)])

    asyncio.run(run_workers())
    assert peak == 2


def test_rpm_window_is_shared_across_calls() -> None:
    limiter = RequestLimiter(
        max_concurrency=1,
        requests_per_minute=1,
        tokens_per_minute=0,
        window_seconds=0.03,
    )
    started = time.monotonic()
    with limiter.slot(1):
        pass
    with limiter.slot(1):
        pass
    assert time.monotonic() - started >= 0.02


def test_tpm_window_is_shared_across_calls() -> None:
    limiter = RequestLimiter(
        max_concurrency=1,
        requests_per_minute=0,
        tokens_per_minute=5,
        window_seconds=0.03,
    )
    started = time.monotonic()
    with limiter.slot(3):
        pass
    with limiter.slot(3):
        pass
    assert time.monotonic() - started >= 0.02


def test_single_request_cannot_exceed_tpm_capacity() -> None:
    limiter = RequestLimiter(
        max_concurrency=1,
        requests_per_minute=0,
        tokens_per_minute=10,
    )
    with pytest.raises(ValueError, match="超过每分钟限额"):
        with limiter.slot(11):
            pass


def test_factory_shares_process_limiter_per_model_role() -> None:
    common = {
        "api_key": "test",
        "model_id": "test",
        "max_concurrency": 2,
        "requests_per_minute": 0,
        "tokens_per_minute": 0,
    }
    first = create_llm(LLMConfig(**common))
    second = create_llm(LLMConfig(**common))
    vision = create_vlm(VLMConfig(**common))

    assert first.request_limiter is second.request_limiter
    assert first.request_limiter is not vision.request_limiter
