"""Agent lifecycle hook primitives.

Exposes key AG-UI event points as async lifecycle methods that the agent
runner invokes. Custom hooks subclass :class:`AgentHook` and override the
handful of methods they care about; :class:`CompositeHook` fans out to
multiple hooks with per-hook error isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wiki_agent.log import get_logger

logger = get_logger("HOOK")

# ────────────────────────────────────────────────────────────────
#  Context — unified turn-level context for all hooks
# ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RunContext:
    """Turn‑level context passed to every hook callback.

    Created by the agent at the start of ``_run()``, shared across all
    iterations within a single turn.  Hooks read (and may mutate) it as
    the turn progresses.
    """

    session_key: str
    """The session this turn belongs to."""

    final_content: str | None = None
    """Last assistant text response, if any."""

    tools_used: list[str] = field(default_factory=list)
    """Distinct tool names called during the turn."""

    usage: dict[str, int] = field(default_factory=dict)
    """Cumulative token usage for the turn."""

    stop_reason: str | None = None
    """Reason the turn ended."""

    error: str | None = None
    """Fatal error that terminated the turn, if any."""

    exception: BaseException | None = None
    """The underlying exception when ``error`` is set."""


# ────────────────────────────────────────────────────────────────
#  AgentHook
# ────────────────────────────────────────────────────────────────


class AgentHook:
    """Lifecycle surface for agent‑run customisation.

    Subclass and override the async methods you need.  Every method
    receives the same :class:`RunContext` and returns ``None``.

    Typical mapping to AG‑UI events (implementations emit these):

    ======================= =================================
    Hook method              AG‑UI event
    ======================= =================================
    ``on_run_start``         ``RUN_STARTED``
    ``on_run_end``           ``RUN_FINISHED``
    ``on_run_error``         ``RUN_ERROR``
    ``on_iteration_start``   ``STEP_STARTED``
    ``on_iteration_end``     ``STEP_FINISHED``
    ``on_stream_delta``      ``TEXT_MESSAGE_CONTENT``
    ``on_stream_end``        ``TEXT_MESSAGE_END``
    ``on_tool_call_start``   ``TOOL_CALL_START / TOOL_CALL_ARGS``
    ``on_tool_result``       ``TOOL_CALL_RESULT``
    ``on_tool_error``        ``TOOL_CALL_RESULT`` (error case)
    ``on_reasoning_start``   ``THINKING_START``
    ``on_reasoning_end``     ``THINKING_END``
    ``finalize_content``      post‑processor (no event)
    ======================= =================================
    """

    __slots__ = ("_reraise",)

    def __init__(self, *, reraise: bool = False) -> None:
        """*reraise* – when True, exceptions from this hook propagate
        to the agent loop instead of being logged and swallowed."""
        self._reraise = reraise

    # ── Run scope ──────────────────────────────────────────

    async def on_status(self, context: RunContext, status: str) -> None:
        """Agent 状态变更通知。

        status 取值:
        - "compacting"   正在压缩历史
        - "thinking"     等待 LLM 响应
        - "idle"         空闲（等待用户输入）
        """
        pass

    async def on_run_start(self, context: RunContext) -> None:
        """Called once before the agent loop begins."""

    async def on_run_end(self, context: RunContext) -> None:
        """Called once after the agent loop finishes (success path)."""

    async def on_run_error(self, context: RunContext) -> None:
        """Called once when the agent loop terminates with an error."""

    # ── Iteration scope ────────────────────────────────────

    async def on_iteration_start(self, context: RunContext) -> None:
        """Called at the top of each agent loop iteration."""

    async def on_iteration_end(self, context: RunContext) -> None:
        """Called at the bottom of each agent loop iteration."""

    # ── Stream scope ───────────────────────────────────────

    async def on_stream_delta(self, context: RunContext, delta: str) -> None:
        """Called for each chunk of streamed LLM text output."""

    async def on_stream_end(self, context: RunContext) -> None:
        """Called when the full streaming response has been assembled."""

    # ── Tool scope ─────────────────────────────────────────

    async def on_tool_call_start(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> None:
        """Called immediately before a single tool is invoked."""

    async def on_tool_result(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        result: Any,
    ) -> None:
        """Called after a single tool invocation succeeds."""

    async def on_tool_error(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        error: Any,
    ) -> None:
        """Called when a tool invocation raises an exception."""

    # ── Reasoning scope ────────────────────────────────────

    async def on_reasoning_start(self, context: RunContext) -> None:
        """Called when the LLM begins emitting reasoning / thinking content."""

    async def on_reasoning_delta(self, context: RunContext, delta: str) -> None:
        """Called for each chunk of reasoning / thinking text."""

    async def on_reasoning_end(self, context: RunContext) -> None:
        """Called when the reasoning stream has finished."""

    # ── Post‑processing ────────────────────────────────────

    def finalize_content(self, content: str | None) -> str | None:
        """Called to transform the final response text before it is
        returned to the caller. Return the (possibly modified) text.

        Unlike the other methods this is synchronous — it runs as a
        pipeline, not a callback.
        """
        return content


# ────────────────────────────────────────────────────────────────
#  CompositeHook
# ────────────────────────────────────────────────────────────────


class CompositeHook(AgentHook):
    """Fan‑out hook that delegates to an ordered list of child hooks.

    Each async method iterates over the children, catching and logging
    exceptions per‑child (unless that child has ``_reraise=True``).
    ``finalize_content`` is a pipeline — each child's output feeds the
    next, and exceptions **do** propagate there on purpose.
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    # ── helpers ──

    async def _fanout(self, method: Any, *args: Any, **kwargs: Any) -> None:
        """逐 hook 扇出——传入绑定方法（非字符串），编译期保证存在。

        字符串分发（getattr(h, "on_x")）是运行时反射：方法名改错
        不报错、拼写错误静默丢失。绑定方法调用让改名/删除在
        定义处直接暴露。
        """
        for h in self._hooks:
            if h._reraise:
                await method(h, *args, **kwargs)
                continue
            try:
                await method(h, *args, **kwargs)
            except Exception:
                logger.exception(
                    "AgentHook.%s 失败在 %s 中",
                    getattr(method, "__name__", "?"), type(h).__name__,
                )

    # ── run ──

    async def on_status(self, c: RunContext, status: str) -> None:
        await self._fanout(AgentHook.on_status, c, status)

    async def on_run_start(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_run_start, c)

    async def on_run_end(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_run_end, c)

    async def on_run_error(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_run_error, c)

    # ── iteration ──

    async def on_iteration_start(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_iteration_start, c)

    async def on_iteration_end(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_iteration_end, c)

    # ── stream ──

    async def on_stream_delta(self, c: RunContext, delta: str) -> None:
        await self._fanout(AgentHook.on_stream_delta, c, delta)

    async def on_stream_end(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_stream_end, c)

    # ── tools ──

    async def on_tool_call_start(
        self, c: RunContext, name: str, tid: str, args: dict[str, Any]
    ) -> None:
        await self._fanout(AgentHook.on_tool_call_start, c, name, tid, args)

    async def on_tool_result(
        self, c: RunContext, name: str, tid: str, result: Any
    ) -> None:
        await self._fanout(AgentHook.on_tool_result, c, name, tid, result)

    async def on_tool_error(
        self, c: RunContext, name: str, tid: str, error: Any
    ) -> None:
        await self._fanout(AgentHook.on_tool_error, c, name, tid, error)

    # ── reasoning ──

    async def on_reasoning_start(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_reasoning_start, c)

    async def on_reasoning_delta(self, c: RunContext, delta: str) -> None:
        await self._fanout(AgentHook.on_reasoning_delta, c, delta)

    async def on_reasoning_end(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_reasoning_end, c)

    # ── pipeline (no isolation) ──

    def finalize_content(self, content: str | None) -> str | None:
        for h in self._hooks:
            content = h.finalize_content(content)
        return content
