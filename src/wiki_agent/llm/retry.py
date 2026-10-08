"""LLM 调用重试层，编译路径与 agent 路径共用。

- 编译路径（async_invoke_with_retry）：校验输出并自动重试。校验失败时追加
  修正消息让 LLM 重新输出，空响应重发；耗尽后包装成调用失败异常。
- 通用路径（retry_llm_call）：重试一次任意 LLM 调用（非流式/流式），不判定
  响应内容，耗尽原样抛出异常。

公共规则：瞬时失败指数退避，FatalError 立即失败，CancelledError 原样传播。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from openai.types.chat import ChatCompletionToolParam
from openai.types.chat.completion_create_params import ResponseFormat

from wiki_agent.config import RetryConfig
from wiki_agent.conversation import LLMResponse, Message
from wiki_agent.errors import FatalError, RetryableError
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import get_logger, span

logger = get_logger("LLM_RETRY")


OutputCheck = Callable[[str], tuple[bool, str]]
_Classify = Callable[[LLMResponse, int], tuple[bool, str, str]]
_OnRetry = Callable[[int, int, BaseException], Awaitable[None]]
_OnRejected = Callable[[LLMResponse, str, str, bool], None]


class _Exhausted(Exception):
    """最后一次尝试抛异常时，包裹原始异常抛出。

    FatalError/CancelledError 原样直抛、不经本异常。两个调用方各自决定耗尽
    行为：async_invoke_with_retry 可返回此前的校验失败响应或抛 RuntimeError，
    retry_llm_call 还原原始异常。
    """

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


async def _sleep_backoff(attempt: int, base_delay: float) -> None:
    """指数退避睡眠，延迟 = 2^attempt * base_delay。

    Args:
        attempt: 已失败的尝试次数（从 0 起）。
        base_delay: 基础延迟（秒）。
    """
    await asyncio.sleep(base_delay * (2**attempt))


async def _retry_core(
    call: Callable[[], Awaitable[LLMResponse]],
    *,
    attempts: int,
    delay: float,
    on_retry: _OnRetry | None = None,
    classify: _Classify | None = None,
    on_rejected: _OnRejected | None = None,
) -> LLMResponse:
    """重试内核：异常分类 + 指数退避 + 共享尝试预算。

    attempts 为总尝试次数，异常重试与响应拒绝（classify 不通过）共用，
    两类预算不相乘。

    Args:
        call: 零参异步可调用（闭包可捕获可变消息列表，重试时看到已追加的修正消息）。
        attempts: 总尝试次数（≥1，RetryConfig 边界已校验）。
        delay: 退避基础延迟（秒）。
        on_retry: 决定重试后、退避前触发的回调。
        classify: 响应判定；None 表示接受任何成功返回的响应。
        on_rejected: 响应被拒时触发（含最后一次尝试）；是否追加修正消息
            由回调按 will_retry 决定。

    Raises:
        asyncio.CancelledError / FatalError: 原样直抛，不 sleep 不重试。
        _Exhausted: 最后一次尝试抛异常时包裹原始异常抛出。
    """
    for attempt in range(attempts):
        async with span("llm_attempt", attempt=attempt + 1, max_attempts=attempts) as s:
            try:
                response = await call()
            except asyncio.CancelledError:
                # 任务取消不属于调用失败，直接传播，不重试
                s.mark_failure("cancelled")
                raise
            except FatalError as exc:
                # 401/404/400 等不可恢复，不重试
                logger.error("Fatal 错误，放弃重试: %s", exc)
                s.mark_failure("fatal")
                raise
            except RetryableError as exc:
                # 限流/超时/网络抖动，指数退避后重试
                logger.warning("调用失败 %d/%d: %s", attempt + 1, attempts, exc)
                s.mark_failure("retryable")
                if attempt == attempts - 1:
                    raise _Exhausted(exc) from exc
                if on_retry is not None:
                    await on_retry(attempt + 1, attempts, exc)
                await _sleep_backoff(attempt, delay)
                continue
            except Exception as exc:
                logger.warning(
                    "未知异常 %d/%d: %s", attempt + 1, attempts, f"{type(exc).__name__}: {exc}"
                )
                s.mark_failure("unknown")
                if attempt == attempts - 1:
                    raise _Exhausted(exc) from exc
                if on_retry is not None:
                    await on_retry(attempt + 1, attempts, exc)
                await _sleep_backoff(attempt, delay)
                continue

            if classify is None:
                return response
            ok, code, reason = classify(response, attempt + 1)
            if ok:
                return response
            s.mark_failure(code)
            s.set_attr("finish_reason", response.finish_reason)
            s.set_attr("content_len", len(response.content))
            if code == "check_failed":
                s.set_attr("check_reason", reason[:120])
            will_retry = attempt < attempts - 1
            if on_rejected is not None:
                on_rejected(response, code, reason, will_retry)
            if not will_retry:
                return response
            await _sleep_backoff(attempt, delay)
    raise RuntimeError("重试内核循环意外退出（attempts 必须 ≥ 1）")  # pragma: no cover


async def retry_llm_call(
    call: Callable[[], Awaitable[LLMResponse]],
    *,
    retry_config: RetryConfig,
    on_retry: _OnRetry | None = None,
) -> LLMResponse:
    """重试一次任意 LLM 调用（async_invoke / async_stream 闭包皆可）。

    不判定响应内容：空内容 + 纯 tool_calls 是 ReAct 的合法回合，不能按编译
    路径的空响应标准判失败重试。任何成功返回的响应都接受。

    Args:
        call: 零参异步可调用（async_stream/async_invoke 闭包）。
        retry_config: 提供 llm_max_attempts / llm_base_delay_seconds。
        on_retry: 重试前回调（UI 提示等），由调用方决定语义。

    Returns:
        call 首次成功的 LLMResponse。

    Raises:
        BaseException: 重试耗尽后原样抛出最后一次尝试的异常，不额外包装。
    """
    try:
        return await _retry_core(
            call,
            attempts=retry_config.llm_max_attempts,
            delay=retry_config.llm_base_delay_seconds,
            on_retry=on_retry,
        )
    except _Exhausted as ex:
        raise ex.original from None


async def async_invoke_with_retry(
    client: LLMClient,
    messages: list[Message],
    *,
    check: OutputCheck | None = None,
    tools: list[ChatCompletionToolParam] | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.5,
    extra_body: dict | None = None,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    response_format: ResponseFormat | None = None,
) -> LLMResponse:
    """编译路径：带输出校验 + 自动重试的 LLM 调用。

    ``check(content) -> (ok, reason)``:
    校验 LLM 输出。ok=False 时，将 reason 追加到对话，让 LLM 修正后重试。

    三种重试条件（共享同一 attempts 预算，实现见 _retry_core）:
    1. LLM 调用异常（timeout / 网络错误）
    2. 空响应（如 reasoning 耗尽输出空间；不追加修正消息，只重发）
    3. check() 返回 ok=False

    max_attempts 语义: 总尝试次数，不是失败后重试次数。
    max_attempts=2 = 最多 2 次尝试 = 1 次重试机会。

    Args:
        client: LLM 客户端。
        messages: 消息列表（校验失败时内部追加修正消息，不修改入参）。
        check: 输出校验回调 ``(content) -> (ok, reason)``。
        tools: OpenAI 工具 schema 列表。
        max_tokens: 生成 token 上限。
        temperature: 采样温度。
        extra_body: 附加请求体参数。
        max_attempts: 总尝试次数（不是重试次数）。
        base_delay: 退避基础延迟（秒）。
        response_format: API 级输出格式约束，透传给客户端。

    Returns:
        最后一次 LLMResponse；即使最终仍未通过校验，check_ok/check_reason
        携带校验结果，调用方据此处理。

    Raises:
        RuntimeError: 所有尝试都抛异常（无任何响应可返回）时；
        FatalError/CancelledError 不经耗尽包装、原样直抛。
    """
    # 预算来源：显式实参优先，缺省读 client.retry_config，不做额外默认。
    # 用局部名而非回写形参：形参声明为 int|None，回写后 pyright 仍按 Optional
    # 推断，局部新名才能被推断为具体数值。
    if max_attempts is None or base_delay is None:
        retry_config = client.retry_config
        attempts = max_attempts if max_attempts is not None else retry_config.llm_max_attempts
        delay = base_delay if base_delay is not None else retry_config.llm_base_delay_seconds
    else:
        attempts = max_attempts
        delay = base_delay
    msgs = list(messages)
    # classify 记录每次拿到响应的尝试，耗尽时据此决定返回最后的
    # 校验失败响应还是抛 RuntimeError（约定见 docstring）。
    last_response: list[LLMResponse] = []

    def _classify(response: LLMResponse, attempt_no: int) -> tuple[bool, str, str]:
        last_response.append(response)
        content = response.content

        if not content.strip():
            # reasoning 模型思考耗尽 max_tokens 时 content 为空，
            # 表现为 finish_reason=length 且 reasoning_content 非空
            rc_len = len(response.reasoning_content or "")
            logger.warning(
                "空响应 %d/%d (finish=%s, reasoning=%d chars)",
                attempt_no,
                attempts,
                response.finish_reason,
                rc_len,
            )
            # check 未被调用，必须显式置 check_ok=False，
            # 否则调用方按默认值把空内容当成通过
            response.check_ok = False
            response.check_reason = (
                f"输出为空（finish_reason={response.finish_reason}）——"
                f"请输出完整内容，不要只输出思考。"
            )
            return False, "empty", response.check_reason

        if check is None:
            return True, "", ""

        ok, reason = check(content)
        # check 结果记入 response，调用方不再重复调用 check
        response.check_ok = ok
        response.check_reason = "" if ok else reason
        if ok:
            return True, "", ""

        usage = response.usage or {}
        logger.warning(
            "校验失败 %d/%d: %s（finish=%s, content_len=%d, "
            "completion_tokens=%s, prompt_tokens=%s, "
            "cache_hit=%s, cache_miss=%s）",
            attempt_no,
            attempts,
            reason[:120],
            response.finish_reason,
            len(content),
            usage.get("completion"),
            usage.get("prompt"),
            usage.get("cache_hit"),
            usage.get("cache_miss"),
        )
        return False, "check_failed", reason

    def _on_rejected(response: LLMResponse, code: str, reason: str, will_retry: bool) -> None:
        # 修正消息只用于 check_failed；空响应重发同一请求即可，
        # 追加 assistant 空消息 + 修正指令反而干扰后续输出。
        if code != "check_failed" or not will_retry:
            return
        msgs.append(Message(role="assistant", content=response.content))
        msgs.append(
            Message(
                role="user",
                content=(f"上一次输出有问题，请修正后重新输出。问题是: {reason}"),
            )
        )

    try:
        return await _retry_core(
            lambda: client.async_invoke(
                msgs,
                **{
                    "tools": tools,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "extra_body": extra_body,
                    # 仅在需要时传，兼容只实现基础签名的测试替身
                    **({"response_format": response_format} if response_format else {}),
                },
            ),
            attempts=attempts,
            delay=delay,
            classify=_classify,
            on_rejected=_on_rejected,
        )
    except _Exhausted as ex:
        # 最后一次尝试抛异常，但此前拿到过校验失败的响应：
        # 返回该响应，调用方读 check_ok 处理。
        if last_response:
            return cast(LLMResponse, last_response[-1])
        # 全部尝试都抛异常 → RuntimeError，消息格式按异常类型区分。
        exc = ex.original
        last_error = str(exc) if isinstance(exc, RetryableError) else f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"LLM 调用 {attempts} 次均失败 - {last_error}") from None
