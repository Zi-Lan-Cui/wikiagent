"""LLM 调用 + 输出校验的唯一收口：重试穷尽即显式抛错。

各编译阶段此前逐字重复"await async_invoke_with_retry → check_ok 检查 →
IngestError(output_validation)"约 15 行/处；收敛到本函数后，调用点只剩
"传参与文案"，不再存在漏查 check_ok 的余地（rewrite 曾漏网，P0 修）。
"""

from __future__ import annotations

from typing import Any

from wiki_agent.conversation import LLMResponse, Message
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.llm.retry import async_invoke_with_retry


async def invoke_checked(
    llm: Any,
    *,
    action: str,
    messages: list[Message],
    stage: IngestStage | None = None,
    source: str = "",
    retry_policy: str = "auto_retry",
    raw: str | None = None,
    error: type[Exception] | None = None,
    **call_kwargs: Any,
) -> LLMResponse:
    """带 check 的调用；重试穷尽仍不过即抛错，不给静默降级的余地。

    Args:
        llm: LLM 客户端。
        action: 报错文案里的阶段名（如 "search"、"路由"）。
        messages: 发送给模型的消息列表。
        stage: 失败归属的编译阶段（issue 记账与诊断用）；走领域异常时可省。
        source: 失败挂账源标识。
        retry_policy: 抛错携带的重试策略标签。
        raw: 失败现场原文；缺省取 response.content。仅 IngestError 分支携带。
        error: 领域异常类（构造签名 (str)，如 RouteError）；给出时以它抛错，
            代替 IngestError——维护线的失败计数与处置不经过 issue 挂账。
        **call_kwargs: 透传 async_invoke_with_retry（check/max_tokens/…）。

    Returns:
        校验通过的 LLMResponse。
    """
    response = await async_invoke_with_retry(llm, messages, **call_kwargs)
    if not response.check_ok:
        detail = f"{action} 输出校验失败（重试后仍失败）: {response.check_reason}"
        if error is not None:
            raise error(detail)
        assert stage is not None, "invoke_checked 必须给 stage 或领域 error"
        raise IngestError(
            stage,
            detail,
            source=source,
            raw=raw if raw is not None else response.content,
            error_code="output_validation",
            error_class="transient",
            retry_policy=retry_policy,
        )
    return response
