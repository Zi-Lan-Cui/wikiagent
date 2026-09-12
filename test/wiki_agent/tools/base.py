from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Literal

from wiki_agent.errors import (
    HandleableError,
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
    
    name: str
    description: str
    parameters: dict

    # 工具的副作用等级是重试策略的输入，不是工具名称的约定。
    # 默认只读：没有声明写入副作用的工具可以安全地被重试。
    side_effect: ClassVar[ToolSideEffect] = "read_only"
    # 总尝试次数（包含首次调用）。只读工具默认最多 2 次；写工具
    # 只有在显式声明参数名并收到 key 时才允许进入同一重试路径。
    retry_attempts: ClassVar[int] = 2
    idempotency_key_param: ClassVar[str | None] = None
    retry_base_delay: ClassVar[float] = 0.2
    timeout_seconds: float = 30.0  # 实例级（MCP 逐 server 配超时），静态工具用类级默认
    breaker_failure_threshold: ClassVar[int] = 5
    breaker_recovery_seconds: ClassVar[float] = 30.0
    breaker_key: ClassVar[str | None] = None

    def resilience_policy(self) -> ToolResiliencePolicy:
        """返回当前工具的统一执行策略。"""
        return ToolResiliencePolicy(
            side_effect=self.side_effect,
            timeout_seconds=self.timeout_seconds,
            # 是否允许本次重试依赖调用参数（尤其是幂等 key），由
            # Registry 在拿到 params 后再判断；这里仅返回工具上限。
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

        工具的业务错误应调用此方法，而不是各自拼接 ``Error: ...``。
        日志仍由边界负责记录；这里的内容只包含 LLM 能据此采取行动的
        安全信息，不包含堆栈和内部路径。
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
        if isinstance(error, HandleableError):
            return self.error_result(
                "recoverable_error",
                str(error) or "工具执行需要修复",
                next_action="根据错误信息修正参数后再调用；如果仍失败，请停止重复调用。",
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

    @classmethod
    def openai_schema(cls):
        """构造 OpenAI 工具 schema。

        Returns:
            注册/调用用 schema 字典（name/description/parameters）。
        """
        return {
            "type": "function",
            "function": {
                "name": cls.name,
                "description": cls.description,
                "parameters": cls.parameters,
            },
        }
