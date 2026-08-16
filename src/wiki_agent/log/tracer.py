"""Trace 传播层——trace_id + span，基于 contextvars。

- trace_id: 一次 agent.run() 一个，贯穿 LLM 调用/工具执行/压缩全链路
- span: 嵌套计时区间，自动记录起止和耗时到事件日志
- contextvars: async 安全——并发任务互不串扰

用法::

    async with span("llm_call", model="deepseek-v4-flash"):
        response = await llm.async_invoke(...)
    # 退出时自动 emit: {"event": "llm_call", "dur_ms": ..., "status": "ok"}
"""

from __future__ import annotations

import contextvars
import time
from typing import Any

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wiki_trace_id", default=None,
)


def current_trace_id() -> str | None:
    """返回当前 context 的 trace_id。

    Returns:
        当前 trace_id；未开启 trace 时返回 None。
    """
    return _trace_id_var.get()


def begin_trace(trace_id: str | None = None) -> str:
    """开启新 trace（一次 agent.run 一个）。

    Args:
        trace_id: 外部注入的 trace_id；None 时按时间戳生成。

    Returns:
        本次 trace 的 trace_id。
    """
    tid = trace_id or f"trace_{int(time.time() * 1000)}"
    _trace_id_var.set(tid)
    return tid


class span:
    """嵌套计时区间——退出时自动 emit 事件到结构化日志。

    用法::

        async with span("llm_call", model="x"):
            ...

    成功:  emit {"event": "llm_call", "dur_ms": 123, "status": "ok", ...attrs}
    异常:  emit {"event": "llm_call", "dur_ms": 123, "status": "error", "error": "..."}
          然后 re-raise（span 只观察，不吞异常）
    """

    __slots__ = ("_event", "_attrs", "_started", "_status", "_error")

    def __init__(self, event: str, **attrs: Any):
        """初始化 span。

        Args:
            event: 事件名（退出时以此名 emit）。
            **attrs: 随事件记录的静态属性。
        """
        self._event = event
        self._attrs = attrs
        self._started = 0.0
        self._status = "ok"
        self._error: str | None = None

    async def __aenter__(self) -> "span":
        self._started = time.perf_counter()
        return self

    # ── 观测属性写入接口（替代直接访问 _attrs 的跨模块耦合）──

    def set_attr(self, key: str, value: Any) -> None:
        """补充观测属性——span 运行中收集上下文（成功数/游标等）。

        Args:
            key: 属性名。
            value: 属性值。
        """
        self._attrs[key] = value

    def mark_failure(self, reason: str) -> None:
        """标记失败——异常外的失败路径（空响应/校验失败）用。

        Args:
            reason: 失败原因描述。
        """
        self._attrs["failure"] = reason

    async def __aexit__(self, exc_type, exc, tb) -> None:
        dur_ms = (time.perf_counter() - self._started) * 1000
        if exc_type is not None:
            self._status = "error"
            self._error = f"{exc_type.__name__}: {exc}"

        from wiki_agent.log.events import emit_event
        emit_event(
            self._event,
            dur_ms=round(dur_ms, 1),
            status=self._status,
            **({"error": self._error} if self._error else {}),
            **self._attrs,
        )
        # 不吞异常——re-raise
        return False
