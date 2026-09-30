"""Agent 生命周期钩子基元。

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


@dataclass(slots=True)
class RunContext:
    """Turn‑level context passed to every hook callback.

    Created by the agent at the start of ``_run()``, shared across all
    iterations within a single turn.  Hooks read (and may mutate) it as
    the turn progresses.
    """

    session_key: str
    """The session this turn belongs to."""

    run_id: str = ""
    """Unique identifier for this Agent turn."""

    sequence: int = 0
    """Monotonic event sequence, incremented by event publishers."""

    def next_sequence(self) -> int:
        """Return the next event sequence number for this run."""
        self.sequence += 1
        return self.sequence

    final_content: str | None = None
    """Last assistant text response, if any."""

    tools_used: list[str] = field(default_factory=list)
    """Distinct tool names called during the turn."""

    stop_reason: str | None = None
    """Reason the turn ended."""

    error: str | None = None
    """Fatal error that terminated the turn, if any."""

    exception: BaseException | None = None
    """The underlying exception when ``error`` is set."""


@dataclass(slots=True)
class CommandProgress:
    """统一的命令进度事件；业务命令只提供阶段和展示文字。"""

    task_id: str
    command: str
    stage: str
    current: int | None = None
    total: int | None = None
    message: str = ""
    level: str = "info"
    data: dict[str, Any] = field(default_factory=dict)


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
    ``on_stream_delta``      ``TEXT_MESSAGE_CONTENT``
    ``on_tool_call_start``   ``TOOL_CALL_START / TOOL_CALL_ARGS``
    ``on_tool_result``       ``TOOL_CALL_RESULT``
    ``on_tool_error``        ``TOOL_CALL_RESULT`` (error case)
    ``on_reasoning_start``   ``THINKING_START``
    ``on_reasoning_end``     ``THINKING_END``
    ======================= =================================
    """

    # Run scope

    async def on_status(self, context: RunContext, status: str) -> None:
        """Agent 状态变更通知。

        Args:
            context: 回合级上下文。
            status: 状态值——"compacting" 正在压缩历史；
                "thinking" 等待 LLM 响应；"idle" 空闲。
        """
        pass

    async def on_run_start(self, context: RunContext) -> None:
        """Called once before the agent loop begins.

        Args:
            context: 回合级上下文。
        """

    async def on_run_end(self, context: RunContext) -> None:
        """Called once after the agent loop finishes (success path).

        Args:
            context: 回合级上下文。
        """

    async def on_run_error(self, context: RunContext) -> None:
        """Called once when the agent loop terminates with an error.

        Args:
            context: 回合级上下文。
        """

    async def on_command_start(
        self,
        context: RunContext,
        command: str,
        task_id: str,
    ) -> None:
        """命令开始执行。"""

    async def on_command_progress(
        self,
        context: RunContext,
        progress: CommandProgress,
    ) -> None:
        """命令阶段进度；message 可按命令自定义。"""

    async def on_command_end(
        self,
        context: RunContext,
        command: str,
        task_id: str,
        result: Any,
    ) -> None:
        """命令成功或业务层返回结果后的收尾通知。"""

    async def on_command_error(
        self,
        context: RunContext,
        command: str,
        task_id: str,
        error: Any,
    ) -> None:
        """命令抛出异常。"""

    async def on_command_cancelled(
        self,
        context: RunContext,
        command: str,
        task_id: str,
    ) -> None:
        """命令被取消。"""

    # Stream scope

    async def on_stream_delta(self, context: RunContext, delta: str) -> None:
        """Called for each chunk of streamed LLM text output.

        Args:
            context: 回合级上下文。
            delta: 本次增量文本块。
        """

    # Tool scope

    async def on_tool_call_start(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> None:
        """Called immediately before a single tool is invoked.

        Args:
            context: 回合级上下文。
            tool_name: 工具名。
            tool_call_id: LLM 侧的工具调用 id（与结果配对）。
            arguments: 工具调用的参数字典。
        """

    async def on_tool_result(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        result: Any,
    ) -> None:
        """Called after a single tool invocation succeeds.

        Args:
            context: 回合级上下文。
            tool_name: 工具名。
            tool_call_id: 工具调用 id。
            result: 工具返回值（转字符串后进入消息）。
        """

    async def on_tool_error(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        error: Any,
    ) -> None:
        """Called when a tool invocation raises an exception.

        Args:
            context: 回合级上下文。
            tool_name: 工具名。
            tool_call_id: 工具调用 id。
            error: 捕获到的异常对象。
        """

    # Reasoning scope

    async def on_reasoning_start(self, context: RunContext) -> None:
        """Called when the LLM begins emitting reasoning / thinking content.

        Args:
            context: 回合级上下文。
        """

    async def on_reasoning_delta(self, context: RunContext, delta: str) -> None:
        """Called for each chunk of reasoning / thinking text.

        Args:
            context: 回合级上下文。
            delta: 本次增量思考文本块。
        """

    async def on_reasoning_end(self, context: RunContext) -> None:
        """Called when the reasoning stream has finished.

        Args:
            context: 回合级上下文。
        """

class CompositeHook(AgentHook):
    """Fan‑out hook that delegates to an ordered list of child hooks.

    Each async method iterates over the children, catching and logging
    exceptions per‑child.
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    # helpers

    async def _fanout(self, method: Any, *args: Any, **kwargs: Any) -> None:
        """逐 hook 扇出调用。

        传入基类方法对象只为在定义处钉住方法名（改名时所有 _fanout
        调用点直接报错，不像字符串那样静默丢失）；实际派发必须走
        getattr(h, name)——AgentHook.on_x(h, ...) 是显式绑死基类实现，
        绕过子类的 MRO 覆写，会让全部子 hook 静默失效。

        Args:
            method: 基类的同名方法（用作方法名凭证与存在性检查）。
            *args / **kwargs: 透传给每个 hook 的参数。
        """
        name = method.__name__
        for h in self._hooks:
            fn = getattr(h, name)
            try:
                await fn(*args, **kwargs)
            except Exception:
                logger.exception("AgentHook.%s 失败在 %s 中", name, type(h).__name__)

    # run

    async def on_status(self, c: RunContext, status: str) -> None:
        await self._fanout(AgentHook.on_status, c, status)

    async def on_run_start(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_run_start, c)

    async def on_run_end(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_run_end, c)

    async def on_run_error(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_run_error, c)

    async def on_command_start(self, c: RunContext, command: str, task_id: str) -> None:
        await self._fanout(AgentHook.on_command_start, c, command, task_id)

    async def on_command_progress(self, c: RunContext, progress: CommandProgress) -> None:
        await self._fanout(AgentHook.on_command_progress, c, progress)

    async def on_command_end(self, c: RunContext, command: str, task_id: str, result: Any) -> None:
        await self._fanout(AgentHook.on_command_end, c, command, task_id, result)

    async def on_command_error(self, c: RunContext, command: str, task_id: str, error: Any) -> None:
        await self._fanout(AgentHook.on_command_error, c, command, task_id, error)

    async def on_command_cancelled(self, c: RunContext, command: str, task_id: str) -> None:
        await self._fanout(AgentHook.on_command_cancelled, c, command, task_id)

    # stream

    async def on_stream_delta(self, c: RunContext, delta: str) -> None:
        await self._fanout(AgentHook.on_stream_delta, c, delta)

    # tools

    async def on_tool_call_start(
        self, context: RunContext, tool_name: str, tool_call_id: str, arguments: dict[str, Any]
    ) -> None:
        await self._fanout(
            AgentHook.on_tool_call_start, context, tool_name, tool_call_id, arguments
        )

    async def on_tool_result(
        self, context: RunContext, tool_name: str, tool_call_id: str, result: Any
    ) -> None:
        await self._fanout(AgentHook.on_tool_result, context, tool_name, tool_call_id, result)

    async def on_tool_error(
        self, context: RunContext, tool_name: str, tool_call_id: str, error: Any
    ) -> None:
        await self._fanout(AgentHook.on_tool_error, context, tool_name, tool_call_id, error)

    # reasoning

    async def on_reasoning_start(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_reasoning_start, c)

    async def on_reasoning_delta(self, c: RunContext, delta: str) -> None:
        await self._fanout(AgentHook.on_reasoning_delta, c, delta)

    async def on_reasoning_end(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_reasoning_end, c)
