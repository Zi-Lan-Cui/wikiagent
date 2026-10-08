"""Agent 生命周期 hook。

AgentHook 把一次回合的关键节点暴露为异步方法，子类按需覆写；
CompositeHook 将每个回调依次转发给多个子 hook，单个 hook 出错不影响其余。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wiki_agent.log import get_logger

logger = get_logger("HOOK")


@dataclass(slots=True)
class RunContext:
    """单回合上下文，随回合创建，传给该回合的全部 hook 回调；hook 可读写。"""

    session_key: str
    """本回合所属的会话。"""

    run_id: str = ""
    """本次 Agent 回合的唯一标识。"""

    sequence: int = 0
    """事件序号，由事件发布方递增。"""

    def next_sequence(self) -> int:
        """返回本回合的下一个事件序号。"""
        self.sequence += 1
        return self.sequence

    final_content: str | None = None
    """最后一条助手文本回复，可能为空。"""

    tools_used: list[str] = field(default_factory=list)
    """本回合调用过的去重工具名。"""

    stop_reason: str | None = None
    """回合结束的原因。"""

    error: str | None = None
    """终止回合的致命错误描述，可能为空。"""

    exception: BaseException | None = None
    """error 有值时对应的底层异常对象。"""


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
    """Agent 运行生命周期的 hook 基类。

    按需覆写其中的异步方法；每个方法接收同一个 RunContext，返回 None。
    实现方通常把回调映射为对应的事件（如 on_run_start → RUN_STARTED）。
    """

    async def on_status(self, context: RunContext, status: str) -> None:
        """Agent 状态变更通知。

        Args:
            context: 回合级上下文。
            status: 状态值——"compacting" 正在压缩历史；
                "thinking" 等待 LLM 响应；"idle" 空闲。
        """
        pass

    async def on_run_start(self, context: RunContext) -> None:
        """agent 循环开始前调用一次。

        Args:
            context: 回合级上下文。
        """

    async def on_run_end(self, context: RunContext) -> None:
        """agent 循环成功结束后调用一次。

        Args:
            context: 回合级上下文。
        """

    async def on_run_error(self, context: RunContext) -> None:
        """agent 循环因异常终止时调用一次。

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

    async def on_stream_delta(self, context: RunContext, delta: str) -> None:
        """流式 LLM 文本输出的每个分片触发。

        Args:
            context: 回合级上下文。
            delta: 本次增量文本块。
        """

    async def on_tool_call_start(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> None:
        """单个工具即将执行前触发。

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
        """单个工具执行成功后触发。

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
        """单个工具执行抛出异常时触发。

        Args:
            context: 回合级上下文。
            tool_name: 工具名。
            tool_call_id: 工具调用 id。
            error: 捕获到的异常对象。
        """

    async def on_reasoning_start(self, context: RunContext) -> None:
        """LLM 开始输出思考内容时触发。

        Args:
            context: 回合级上下文。
        """

    async def on_reasoning_delta(self, context: RunContext, delta: str) -> None:
        """思考内容的每个增量分片触发。

        Args:
            context: 回合级上下文。
            delta: 本次增量思考文本块。
        """

    async def on_reasoning_end(self, context: RunContext) -> None:
        """思考流结束时触发。

        Args:
            context: 回合级上下文。
        """

class CompositeHook(AgentHook):
    """将每个回调按序转发给一组子 hook。

    逐个遍历子 hook，单个子 hook 的异常被捕获并记录，不影响其余。
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    async def _fanout(self, method: Any, *args: Any, **kwargs: Any) -> None:
        """逐个调用每个子 hook 的同名方法。

        method 传基类方法对象，只是为了在调用处固定方法名（改名即报错，
        不像字符串那样静默失效）；实际派发必须走 getattr(h, name)——
        直接调 AgentHook 的方法会绑定基类实现，绕过子类的覆写。

        Args:
            method: 基类的同名方法（提供方法名）。
            *args / **kwargs: 透传给每个 hook 的参数。
        """
        name = method.__name__
        for h in self._hooks:
            fn = getattr(h, name)
            try:
                await fn(*args, **kwargs)
            except Exception:
                logger.exception("AgentHook.%s 失败在 %s 中", name, type(h).__name__)

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

    async def on_stream_delta(self, c: RunContext, delta: str) -> None:
        await self._fanout(AgentHook.on_stream_delta, c, delta)

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

    async def on_reasoning_start(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_reasoning_start, c)

    async def on_reasoning_delta(self, c: RunContext, delta: str) -> None:
        await self._fanout(AgentHook.on_reasoning_delta, c, delta)

    async def on_reasoning_end(self, c: RunContext) -> None:
        await self._fanout(AgentHook.on_reasoning_end, c)
