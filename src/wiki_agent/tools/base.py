from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Literal

from wiki_agent.errors import (
    RetryableError,
    WikiAgentError,
)

ToolSideEffect = Literal["read_only", "idempotent_write", "irreversible"]


@dataclass(frozen=True)
class ToolResiliencePolicy:
    """工具声明的执行策略；执行器由 ToolRegistry 统一提供。"""

    side_effect: ToolSideEffect = "read_only"
    timeout_seconds: float = 30.0
    max_attempts: int = 2
    base_delay_seconds: float = 0.2
    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    breaker_key: str = ""


def format_tool_error(
    tool: str,
    code: str,
    message: str,
    *,
    next_action: str,
    retryable: bool = False,
) -> str:
    """统一生成工具结果中的可行动错误块。"""
    return "\n".join(
        (
            "[TOOL_ERROR]",
            f"tool: {tool}",
            f"code: {code}",
            f"retryable: {'true' if retryable else 'false'}",
            f"message: {message}",
            f"next_action: {next_action}",
        )
    )


class BaseTool(ABC):
    # 实例级元数据：静态工具在类体赋默认值，MCPToolWrapper 在 __init__
    # 逐实例赋值，故不能声明为 ClassVar（ClassVar 禁止 self 赋值）
    name: str
    description: str
    # 嵌套 dict（type、properties、required），不是字符串
    parameters: dict

    # 默认只读：未声明写入副作用的工具可安全重试
    side_effect: ClassVar[ToolSideEffect] = "read_only"
    # 总尝试次数（含首次）。写工具须声明 idempotency_key_param 并在
    # 调用时传入该 key 才允许重试
    retry_attempts: ClassVar[int] = 2
    idempotency_key_param: ClassVar[str | None] = None
    retry_base_delay: ClassVar[float] = 0.2
    timeout_seconds: float = 30.0  # 实例级，MCP 按 server 配置；静态工具用默认值
    breaker_failure_threshold: ClassVar[int] = 5
    breaker_recovery_seconds: ClassVar[float] = 30.0
    breaker_key: ClassVar[str | None] = None

    def resilience_policy(self) -> ToolResiliencePolicy:
        """返回当前工具的统一执行策略。"""
        return ToolResiliencePolicy(
            side_effect=self.side_effect,
            timeout_seconds=self.timeout_seconds,
            # 是否重试由 Registry 结合调用参数判断，这里只给出上限
            max_attempts=max(1, self.retry_attempts),
            base_delay_seconds=max(0.0, self.retry_base_delay),
            failure_threshold=max(1, self.breaker_failure_threshold),
            recovery_seconds=max(0.0, self.breaker_recovery_seconds),
            breaker_key=self.breaker_key or f"tool:{self.name}",
        )

    def _retry_is_allowed(self, kwargs: dict) -> bool:
        """根据副作用等级决定是否可以重试。"""
        if self.side_effect == "read_only":
            return True
        if self.side_effect == "idempotent_write":
            key_name = self.idempotency_key_param
            return bool(key_name and kwargs.get(key_name))
        return False

    def error_result(
        self,
        code: str,
        message: str,
        *,
        next_action: str,
        retryable: bool = False,
    ) -> str:
        """生成给 LLM 的可行动错误结果。

        工具的业务错误统一走此方法；内容只含 LLM 可据以行动的安全信息，
        不含堆栈与内部路径，日志由调用边界记录。
        """
        return format_tool_error(
            self.name,
            code,
            message,
            next_action=next_action,
            retryable=retryable,
        )

    def _exception_result(self, error: WikiAgentError) -> str:
        """把分类异常转换成 LLM 可执行的诊断。"""
        if isinstance(error, RetryableError):
            return self.error_result(
                "transient_unavailable",
                str(error) or "暂时无法完成工具调用",
                retryable=True,
                next_action="稍后重试；如果问题持续，请换用其他工具或告知用户。",
            )
        return self.error_result(
            "tool_error",
            str(error) or "工具未能完成请求",
            next_action="检查调用参数并尝试其他可行方案。",
        )

    @abstractmethod
    async def execute_once(self, **kwargs) -> str:
        """执行一次业务动作，不处理 timeout、retry、熔断或错误渲染。

        ``ToolRegistry.execute()`` 是唯一公共入口；它统一处理取消、
        timeout、retry、circuit breaker 以及面向 LLM 的错误结果。
        """
        raise NotImplementedError

