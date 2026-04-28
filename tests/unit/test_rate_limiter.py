import pytest

from ai_browser_agent.llm.rate_limiter import TokenRateLimiter


def test_token_rate_limiter_estimates_with_safety_factor_and_max_output() -> None:
    limiter = TokenRateLimiter(limit_per_minute=100, safety_factor=2.0)

    assert limiter.estimate_request_tokens(6_000, 2_048) == 12_000
    assert limiter.estimate_request_tokens(1_000, 4_096) == 5_096


@pytest.mark.asyncio
async def test_token_rate_limiter_waits_until_rolling_window_has_capacity() -> None:
    now = 100.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = TokenRateLimiter(
        limit_per_minute=100,
        safety_factor=1.0,
        clock=clock,
        sleep=sleep,
    )
    limiter.record_usage(90)

    assert limiter.required_wait_seconds(20) == 60.0
    waited = await limiter.wait_for_capacity(20)

    assert waited == 60.0
    assert sleeps == [60.0]
