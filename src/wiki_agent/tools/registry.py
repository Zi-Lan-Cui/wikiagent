import asyncio
from typing import cast

from openai.types.chat import ChatCompletionToolParam

from wiki_agent.errors import (
    FatalError,
    RetryableError,
    WikiAgentError,
    translate_generic_error,
)
from wiki_agent.log import emit_event, get_logger
from wiki_agent.tools.base import BaseTool
from wiki_agent.tools.resilience import CircuitBreaker

logger = get_logger("ToolRegistry")


class ToolRegistry:
    def __init__(self):
        self._tools = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具（同名覆盖）。

        Args:
            tool: 工具实例（name 作 key）。
        """
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具（不存在时静默）。

        Args:
            name: 工具名。
        """
        self._tools.pop(name, None)

    def get(self, name: str):
        """按名取工具实例。

        Args:
            name: 工具名。

        Returns:
            工具实例；未注册返回 None。
        """
        return self._tools.get(name, None)

    async def execute(self, name: str, params: dict) -> str:
        """执行工具调用。

        Args:
            name: 工具名。
            params: 工具参数字典。

        Returns:
            工具结果文本；未注册工具返回错误文本。
        """
        if name not in self._tools:
            return f"Error: 使用了未被注册的工具 {name} "
        tool = self._tools[name]
        policy = tool.resilience_policy()
        breaker = self._breakers.setdefault(
            policy.breaker_key,
            CircuitBreaker(
                failure_threshold=policy.failure_threshold,
                recovery_seconds=policy.recovery_seconds,
            ),
        )
        if not await breaker.allow():
            emit_event(
                "tool_call_rejected",
                tool=name,
                breaker_key=policy.breaker_key,
                reason="circuit_open",
            )
            return tool.error_result(
                "circuit_open",
                "工具依赖服务暂时不可用，调用已被熔断。",
                next_action="稍后再试；不要在熔断期间重复调用相同工具。",
                retryable=True,
            )

        last_error: WikiAgentError | None = None
        result = ""
        can_retry = tool._retry_is_allowed(params)
        if tool.side_effect == "idempotent_write" and not can_retry:
            emit_event("tool_retry_blocked", tool=name, reason="missing_idempotency_key")
        for attempt in range(policy.max_attempts):
            try:
                result = await asyncio.wait_for(
                    tool.execute_once(**params), timeout=policy.timeout_seconds
                )
                # 之前的瞬态失败只代表前一次 attempt；成功后必须清掉，
                # 否则循环结束会把旧错误误报给 LLM。
                last_error = None
                break
            except asyncio.CancelledError:
                emit_event("tool_cancelled", tool=name, attempt=attempt + 1)
                raise
            except TimeoutError as exc:
                last_error = RetryableError(
                    f"工具 {name} 超时（{policy.timeout_seconds:g}s）", cause=exc
                )
            except WikiAgentError as exc:
                last_error = exc
            except Exception as exc:
                last_error = translate_generic_error(exc, context=f"工具 {name}")

            can_retry = (
                isinstance(last_error, RetryableError)
                and attempt < policy.max_attempts - 1
                and can_retry
            )
            if can_retry:
                emit_event(
                    "tool_retry",
                    tool=name,
                    attempt=attempt + 1,
                    max_attempts=policy.max_attempts,
                    error_type=type(last_error).__name__,
                )
                await asyncio.sleep(policy.base_delay_seconds * (2**attempt))
                continue
            break

        if last_error is None:
            await breaker.record_success()
            emit_event("tool_call_succeeded", tool=name, breaker_key=policy.breaker_key)
            return result

        opened = False
        # 只把瞬态基础设施故障计入熔断；参数、权限和业务错误不应熔断。
        if isinstance(last_error, RetryableError):
            opened = await breaker.record_failure()
        else:
            await breaker.record_success()
        if opened:
            emit_event(
                "tool_circuit_opened",
                tool=name,
                breaker_key=policy.breaker_key,
                failure_threshold=policy.failure_threshold,
            )

        if isinstance(last_error, FatalError):
            return tool.error_result(
                "internal_error",
                "工具内部错误，当前请求无法由模型自行修复。",
                next_action="不要重复提交相同请求；请换用其他方案或告知用户。",
            )
        return tool._exception_result(last_error)

    def get_all_description(self) -> str:
        """拼接全部工具描述（供 prompt 使用）。

        Returns:
            "函数名: X, description: Y" 换行拼接文本。
        """
        return "\n".join(
            [f"函数名:{name},description: {tool.description}" for name, tool in self._tools.items()]
        )

    def get_all_schema_openai(self) -> list[ChatCompletionToolParam]:
        """返回全部工具的 OpenAI schema 列表。

        Returns:
            OpenAI 工具 schema 列表（注册表内全部工具）。
        """
        all_schema = []
        for tool in self._tools.values():
            all_schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        # Tool parameters are intentionally stored as JSON-schema dictionaries in
        # BaseTool.  This adapter is the single boundary where they become the
        # OpenAI SDK's TypedDict-based request type.
        return cast(list[ChatCompletionToolParam], all_schema)
