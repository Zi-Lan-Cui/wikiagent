"""Process-local request limiter shared by every call made through one client."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from threading import Condition, Lock


class RequestLimiter:
    """Enforce concurrency, RPM and estimated TPM across sync/async calls.

    The limiter reserves request/token budget before a provider request starts and
    holds the concurrency slot until a streaming response is fully consumed.
    A zero RPM/TPM value disables only that window limit.
    """

    def __init__(
        self,
        *,
        max_concurrency: int,
        requests_per_minute: int,
        tokens_per_minute: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self._window_seconds = window_seconds
        self._clock = clock
        self._budget_lock = Lock()
        self._requests: deque[float] = deque()
        self._tokens: deque[tuple[float, int]] = deque()
        self._concurrency = Condition()
        self._active = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._requests and self._requests[0] <= cutoff:
            self._requests.popleft()
        while self._tokens and self._tokens[0][0] <= cutoff:
            self._tokens.popleft()

    def _reservation_wait(self, estimated_tokens: int) -> float:
        now = self._clock()
        with self._budget_lock:
            self._prune(now)
            waits: list[float] = []
            if self.requests_per_minute and len(self._requests) >= self.requests_per_minute:
                waits.append(self._requests[0] + self._window_seconds - now)

            if self.tokens_per_minute:
                if estimated_tokens > self.tokens_per_minute:
                    raise ValueError(
                        f"单次请求预估 {estimated_tokens} tokens，超过每分钟限额 "
                        f"{self.tokens_per_minute}"
                    )
                used = sum(tokens for _, tokens in self._tokens)
                overflow = used + estimated_tokens - self.tokens_per_minute
                if overflow > 0:
                    released = 0
                    for timestamp, tokens in self._tokens:
                        released += tokens
                        if released >= overflow:
                            waits.append(timestamp + self._window_seconds - now)
                            break

            wait = max(waits, default=0.0)
            if wait <= 0:
                if self.requests_per_minute:
                    self._requests.append(now)
                if self.tokens_per_minute:
                    self._tokens.append((now, estimated_tokens))
                return 0.0
            return wait

    def _reserve_sync(self, estimated_tokens: int) -> None:
        while (wait := self._reservation_wait(estimated_tokens)) > 0:
            time.sleep(wait)

    async def _reserve_async(self, estimated_tokens: int) -> None:
        while (wait := self._reservation_wait(estimated_tokens)) > 0:
            await asyncio.sleep(wait)

    def _acquire_sync(self) -> None:
        with self._concurrency:
            self._concurrency.wait_for(lambda: self._active < self.max_concurrency)
            self._active += 1

    async def _acquire_async(self) -> None:
        while True:
            with self._concurrency:
                if self._active < self.max_concurrency:
                    self._active += 1
                    return
            await asyncio.sleep(0.01)

    def _release(self) -> None:
        with self._concurrency:
            self._active -= 1
            self._concurrency.notify()

    @contextmanager
    def slot(self, estimated_tokens: int) -> Iterator[None]:
        """Reserve a synchronous request slot."""
        self._reserve_sync(estimated_tokens)
        self._acquire_sync()
        try:
            yield
        finally:
            self._release()

    @asynccontextmanager
    async def async_slot(self, estimated_tokens: int) -> AsyncIterator[None]:
        """Reserve an asynchronous request slot."""
        await self._reserve_async(estimated_tokens)
        await self._acquire_async()
        try:
            yield
        finally:
            self._release()
