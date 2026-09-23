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
    """带 handler 的可修复错误——修好再试一次。"""

    def __init__(self, message: str = "", *, handler: Callable[[], Awaitable] | None = None):
        super().__init__(message)
        self._handler = handler

    async def handle(self):
        """执行修复。"""
        if self._handler is None:
            return None
        return await self._handler()


class FatalError(WikiAgentError):
    """配置错误、编程错误、鉴权失败——重试一万次也没用。"""

    def __init__(self, message: str = "", *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


class IngestStage(StrEnum):
    """编译链路各阶段的统一命名。"""

    LOAD = "load"  # 文件发现
    CONVERT = "convert"  # 格式转换
    EXTRACT = "extract"  # 摘要
    SEARCH = "search"  # 候选检索
    ANALYZE = "analyze"  # 关系分析
    PLAN = "plan"  # 决策
    EXECUTE = "execute"  # 页面生成


class IngestError(WikiAgentError):
    """编译链路统一异常——既是流水线内的失败信号，也是汇总的失败记录。"""

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
        self.raw = raw


def translate_openai_error(exc: Exception) -> WikiAgentError:
    """把 OpenAI SDK 异常翻译成三分类。"""
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
    """把非 OpenAI 的通用异常翻译成三分类。"""
    if isinstance(exc, WikiAgentError):
        return exc

    prefix = f"{context}: " if context else ""
    if isinstance(exc, (FileNotFoundError, PermissionError, ValueError, TypeError, KeyError)):
        return FatalError(f"{prefix}{type(exc).__name__}: {exc}", cause=exc)
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return RetryableError(f"{prefix}{type(exc).__name__}: {exc}", cause=exc)
    return FatalError(f"{prefix}未知异常 {type(exc).__name__}: {exc}", cause=exc)
