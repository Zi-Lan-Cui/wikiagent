"""异常分类体系——策略编码在类型里，不在调用点的 if/elif 里。

三分类:
- RetryableError:  限流(429)/超时/网络抖动 → 指数退避原样重试
- HandleableError: 带 handler 可修复 → 修好再试一次 → 再失败视为 Fatal
- FatalError:      配置/编程/鉴权错误 → 不重试不修复，直接炸到边界

LLM 的重试壳是 llm/retry.py::async_invoke_with_retry；工具的重试
策略在 tools/registry.py 边界按副作用等级执行。两者不嵌套：LLM 调用
和工具调用各自只拥有一个重试边界。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum

from wiki_agent.log import get_logger

logger = get_logger("ERRORS")


class WikiAgentError(Exception):
    """所有可预期错误的基类——未分类的异常不继承它。"""


class RetryableError(WikiAgentError):
    """限流、超时、网络抖动——幂等场景下指数退避重试。"""

    def __init__(self, message: str = "", *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


class HandleableError(WikiAgentError):
    """带 handler 的可修复错误——修好再试一次。

    handler: 返回修复后的结果（替代原调用）的协程。

    状态: 定义后零使用——类型体系的三分之一未验证。保留观察
    （TODO 有"实战应用"条目）: FileNotFound→建默认文件这类
    "修好再试"场景在工具/IO 层出现时接入，不提前造场景。
    """

    def __init__(self, message: str = "", *, handler: Callable[[], Awaitable] | None = None):
        super().__init__(message)
        self._handler = handler

    async def handle(self):
        """执行修复。

        Returns:
            handler 执行结果；无 handler 返回 None（视为修复失败）。
        """
        if self._handler is None:
            return None
        return await self._handler()


class FatalError(WikiAgentError):
    """配置错误、编程错误、鉴权失败——重试一万次也没用。"""

    def __init__(self, message: str = "", *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


class IngestStage(StrEnum):
    """编译链路各阶段——统一命名，杜绝魔法字符串。"""

    LOAD = "load"  # 文件发现
    CONVERT = "convert"  # 格式转换
    EXTRACT = "extract"  # 摘要
    SEARCH = "search"  # 候选检索
    ANALYZE = "analyze"  # 关系分析
    PLAN = "plan"  # 决策
    EXECUTE = "execute"  # 页面生成


class IngestError(WikiAgentError):
    """编译链路统一异常——既是流水线内的失败信号，也是汇总的失败记录。

    类型不分阶段: 各阶段失败的处理策略相同（记录→跳过→汇总），
    差异在数据（stage 枚举字段），不在类型。

    - stage:  哪个阶段失败（IngestStage 枚举）
    - source: 哪个源文件（source_identity）
    - cause:  原始异常（传输错误等保留原始类型，未分类失败凭 message 标记）

    "跳过这个文件"是边界的策略，不是这个类型的语义——
    边界统一收集，汇总按 stage 分组，落机器可读清单。
    """

    def __init__(
        self,
        stage: IngestStage,
        message: str = "",
        *,
        source: str = "",
        cause: Exception | None = None,
        raw: str = "",
        error_code: str = "ingest_error",
        error_class: str | None = None,
        retry_policy: str | None = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.source = source
        self.cause = cause
        self.error_code = error_code
        if error_class is None or retry_policy is None:
            if isinstance(cause, RetryableError):
                error_class = error_class or "transient"
                retry_policy = retry_policy or "auto_retry"
            elif isinstance(cause, HandleableError):
                error_class = error_class or "recoverable"
                retry_policy = retry_policy or "retry_once"
            elif isinstance(cause, FatalError):
                error_class = error_class or "permanent"
                retry_policy = retry_policy or "manual"
            else:
                error_class = error_class or "unknown"
                retry_policy = retry_policy or "manual"
        self.error_class = error_class
        self.retry_policy = retry_policy
        # LLM 原始输出（如有）——失败现场数据。
        # 边界在 compile_failure/refine_failure 事件里全量落盘
        # （事件流是唯一机器事实源）。
        self.raw = raw


# ════════════════════════════════════════════════════════════
#  OpenAI/HTTP 异常翻译——把传输层异常映射到我们的类型
# ════════════════════════════════════════════════════════════


def translate_openai_error(exc: Exception) -> WikiAgentError:
    """把 OpenAI SDK 异常翻译成三分类。

    Retryable（重试可能好）:
    - RateLimitError(429) / APITimeoutError / APIConnectionError
    - InternalServerError(500+)——服务端抖动
    - APITimeoutError

    Fatal（重试一万次也没用）:
    - AuthenticationError(401)——API key 错
    - PermissionDeniedError(403)——权限不足
    - NotFoundError(404)——模型名错
    - BadRequestError(400) / UnprocessableEntity(422)——请求本身有问题
    - ConflictError(409)

    未知异常默认 Retryable——网络世界宁可多试一次，
    也不把可能恢复的问题当 fatal 杀死。
    """
    import openai

    if isinstance(
        exc,
        (
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
        ),
    ):
        return RetryableError(f"限流或网络问题: {exc}", cause=exc)
    if isinstance(
        exc,
        (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.NotFoundError,
            openai.BadRequestError,
            openai.UnprocessableEntityError,
            openai.ConflictError,
        ),
    ):
        return FatalError(f"请求本身有问题（检查 API key/模型名/参数）: {exc}", cause=exc)
    return RetryableError(f"LLM 调用异常: {exc}", cause=exc)


def translate_generic_error(exc: Exception, context: str = "") -> WikiAgentError:
    """把非 OpenAI 的通用异常翻译成三分类。

    工具/IO/存储层的裸异常在这里分类:
    - 已经是 WikiAgentError → 原样返回（幂等）
    - FileNotFoundError / PermissionError / ValueError → Fatal（重试无用）
    - OSError / TimeoutError / ConnectionError → Retryable（IO 抖动可能恢复）
    - 未知 → Fatal（未知异常重试可能放大问题，且本来就该暴露出来修）

    context: 调用场景描述，进错误消息便于定位。
    """
    if isinstance(exc, WikiAgentError):
        return exc

    prefix = f"{context}: " if context else ""
    if isinstance(exc, (FileNotFoundError, PermissionError, ValueError, TypeError, KeyError)):
        return FatalError(f"{prefix}{type(exc).__name__}: {exc}", cause=exc)
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return RetryableError(f"{prefix}{type(exc).__name__}: {exc}", cause=exc)
    return FatalError(f"{prefix}未知异常 {type(exc).__name__}: {exc}", cause=exc)
