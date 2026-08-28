"""工具调用的熔断状态机。

这里只管理“下游是否暂时可用”，不决定某个工具的业务 fallback；
重试次数和 timeout 由 ToolResiliencePolicy 提供给 ToolRegistry。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

CircuitState = Literal["closed", "open", "half_open"]


@dataclass(frozen=True)
class CircuitSnapshot:
    state: CircuitState
    failures: int
    opened_at: float | None


class CircuitBreaker:
    """单个工具/下游 key 的进程内熔断器。"""

    def __init__(self, *, failure_threshold: int = 5, recovery_seconds: float = 30.0):
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_seconds = max(0.0, recovery_seconds)
        self._state: CircuitState = "closed"
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        """判断是否允许本次调用；half-open 只放行一个探测请求。"""
        async with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                opened_at = self._opened_at or time.monotonic()
                if time.monotonic() - opened_at < self.recovery_seconds:
                    return False
                self._state = "half_open"
            if self._state == "half_open":
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True
            return False

    async def record_success(self) -> None:
        async with self._lock:
            self._state = "closed"
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    async def record_failure(self) -> bool:
        """记录一次完整执行失败，返回本次是否刚刚打开熔断。"""
        async with self._lock:
            self._probe_in_flight = False
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()
                return True
            self._failures += 1
            if self._failures >= self.failure_threshold:
                just_opened = self._state != "open"
                self._state = "open"
                self._opened_at = time.monotonic()
                return just_opened
            return False

    async def snapshot(self) -> CircuitSnapshot:
        async with self._lock:
            return CircuitSnapshot(self._state, self._failures, self._opened_at)
