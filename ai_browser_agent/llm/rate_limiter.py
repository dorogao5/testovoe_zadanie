from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class TokenRateLimiter:
    limit_per_minute: int = 0
    safety_factor: float = 2.0
    window_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep
    _events: deque[tuple[float, int]] = field(default_factory=deque)

    def estimate_request_tokens(self, prompt_estimate: int, max_output_tokens: int) -> int:
        prompt_estimate = max(1, int(prompt_estimate))
        max_output_tokens = max(0, int(max_output_tokens))
        return max(
            int(prompt_estimate * self.safety_factor),
            prompt_estimate + max_output_tokens,
        )

    async def wait_for_capacity(self, estimated_tokens: int) -> float:
        if self.limit_per_minute <= 0:
            return 0.0
        reservation = min(max(1, int(estimated_tokens)), self.limit_per_minute)
        total_wait = 0.0
        while True:
            now = self.clock()
            self._prune(now)
            consumed = sum(tokens for _, tokens in self._events)
            if consumed + reservation <= self.limit_per_minute:
                return total_wait
            oldest_at, _ = self._events[0]
            wait_seconds = max(0.0, oldest_at + self.window_seconds - now)
            if wait_seconds <= 0:
                self._prune(self.clock())
                continue
            await self.sleep(wait_seconds)
            total_wait += wait_seconds

    def required_wait_seconds(self, estimated_tokens: int) -> float:
        if self.limit_per_minute <= 0:
            return 0.0
        reservation = min(max(1, int(estimated_tokens)), self.limit_per_minute)
        now = self.clock()
        self._prune(now)
        consumed = sum(tokens for _, tokens in self._events)
        if consumed + reservation <= self.limit_per_minute:
            return 0.0
        oldest_at, _ = self._events[0]
        return max(0.0, oldest_at + self.window_seconds - now)

    def record_usage(self, tokens: int) -> None:
        if self.limit_per_minute <= 0:
            return
        tokens = max(1, int(tokens))
        now = self.clock()
        self._prune(now)
        self._events.append((now, tokens))

    def current_usage(self) -> int:
        self._prune(self.clock())
        return sum(tokens for _, tokens in self._events)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()
