from __future__ import annotations

import asyncio
from enum import StrEnum

from wiki_agent.log import get_logger

logger = get_logger("ERRORS")


class WikiAgentError(Exception):
    """可预期错误的基类；未分类异常不继承此类。"""


class RetryableError(WikiAgentError):
    """限流、超时、网络故障等临时错误；幂等操作可按指数退避重试。"""

    def __init__(self, message: str = "", *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


class FatalError(WikiAgentError):
    """配置错误、编程错误、鉴权失败；重试无法恢复。"""

    def __init__(self, message: str = "", *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


class IngestStage(StrEnum):
    """编译流程各阶段名称。"""

    LOAD = "load"  # 文件发现
    CONVERT = "convert"  # 格式转换
    EXTRACT = "extract"  # 摘要
    SEARCH = "search"  # 候选检索
    ANALYZE = "analyze"  # 关系分析
    PLAN = "plan"  # 决策
    EXECUTE = "execute"  # 页面生成


class IngestError(WikiAgentError):
    """编译流程统一异常，携带阶段、来源与重试分类信息。"""

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
    """把 OpenAI SDK 异常映射为 RetryableError 或 FatalError。"""
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
    """把通用异常映射为 RetryableError 或 FatalError；已是 WikiAgentError 的原样返回。"""
    if isinstance(exc, WikiAgentError):
        return exc

    prefix = f"{context}: " if context else ""
    if isinstance(exc, (FileNotFoundError, PermissionError, ValueError, TypeError, KeyError)):
        return FatalError(f"{prefix}{type(exc).__name__}: {exc}", cause=exc)
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return RetryableError(f"{prefix}{type(exc).__name__}: {exc}", cause=exc)
    return FatalError(f"{prefix}未知异常 {type(exc).__name__}: {exc}", cause=exc)


# 错误文本写入受限字段时的截断长度：SUMMARY 用于 jobs.error、
# issue.summary、last_error 等列，DETAIL 用于 detail，TRACE 用于日志与 evidence。
ERROR_SUMMARY_LIMIT = 500
ERROR_DETAIL_LIMIT = 1000
ERROR_TRACE_LIMIT = 200


def summarize_error(text: object, limit: int) -> str:
    """把错误文本压成单行并截断到 limit。"""
    collapsed = " ".join(str(text).split())
    return collapsed[:limit]
