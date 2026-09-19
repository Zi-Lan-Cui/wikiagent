"""LLM 调用重试层——编译链路与 agent 链路的统一重试实现。

- 编译面: 输出校验 + 自动重试——校验失败时追加修正消息让 LLM 重新输出，
  空响应同样重发；耗尽后统一包装成调用失败异常。
- 通用面: 重试一次任意 LLM 调用（非流式/流式皆可），不判定响应内容，
  耗尽原样抛出——调用边界按异常类型决策。

共享语义: 瞬时失败指数退避，Fatal 类立即失败，取消原样传播；
作为中间件运行在客户端之上，不修改客户端。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from openai.types.chat import ChatCompletionToolParam

from wiki_agent.config import RetryConfig
from wiki_agent.conversation import LLMResponse, Message
from wiki_agent.errors import FatalError, RetryableError
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import get_logger, span

logger = get_logger("LLM_RETRY")


# 核心回调签名
OutputCheck = Callable[[str], tuple[bool, str]]
_Classify = Callable[[LLMResponse, int], tuple[bool, str, str]]
_OnRetry = Callable[[int, int, BaseException], Awaitable[None]]
_OnRejected = Callable[[LLMResponse, str, str, bool], None]


class _Exhausted(Exception):
    """最后一次尝试抛异常时，包裹原始异常抛出。

    Fatal/Cancelled 原样直抛、不经哨兵（尤其 FatalError 是 Exception 子类，
    适配面若 except Exception 会把"立即失败"契约吞掉）。两张适配面据此
    各自决定耗尽语义：编译面可能返回此前的校验失败响应或抛 RuntimeError，
    react 面还原原始异常。
    """

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


async def _sleep_backoff(attempt: int, base_delay: float) -> None:
    """指数退避睡眠。

    延迟 = 2^attempt * base_delay（唯一重试内核共用）。

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
    """唯一重试内核：异常分类 + 指数退避 + 共享尝试预算。

    预算 `attempts` 是总尝试次数，异常重试与响应拒绝（classify 不通过）
    共用——不存在"异常×校验"预算相乘。

    Args:
        call: 零参异步可调用（闭包可捕获可变消息列表，重试时看到已追加的修正消息）。
        attempts: 总尝试次数（≥1，RetryConfig 边界已校验）。
        delay: 退避基础延迟（秒）。
        on_retry: 决定重试后、退避前触发的回调。
        classify: 响应判定；None 表示任何成功响应都接受（react 流式路径）。
        on_rejected: 响应被拒时触发（包括最后一次尝试——日志/记录不缺席，
            修正消息追加由 will_retry 门控）。

    Raises:
        asyncio.CancelledError / FatalError: 原样直抛，不 sleep 不重试。
        _Exhausted: 最后一次尝试抛异常时包裹原始异常抛出。
    """
    for attempt in range(attempts):
        async with span("llm_attempt", attempt=attempt + 1, max_attempts=attempts) as s:
            try:
                response = await call()
            except asyncio.CancelledError:
                # 用户/上层任务主动取消，不是 provider 瞬态故障。
                # 不 sleep、不重试、不伪装成普通 LLM 错误，直接传播。
                s.mark_failure("cancelled")
                raise
            except FatalError as exc:
                # 401/404/400——不重试
                logger.error("Fatal 错误，放弃重试: %s", exc)
                s.mark_failure("fatal")
                raise
            except RetryableError as exc:
                # 限流/超时/网络抖动——指数退避重试
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

    与编译面 async_invoke_with_retry 的区别是定位而非兼容：本函数不判定
    响应内容——空内容 + 纯 tool_calls 是 ReAct 的合法回合，不能按编译面的
    空响应标准判失败重试。任何成功返回的响应都接受。

    Args:
        call: 零参异步可调用（async_stream/async_invoke 闭包）。
        retry_config: 提供 llm_max_attempts / llm_base_delay_seconds。
        on_retry: 重试前回调（UI 提示等），由调用方决定语义。

    Returns:
        call 首次成功的 LLMResponse。

    Raises:
        BaseException: 重试耗尽后原样抛出最后一次尝试的异常——turn 边界
        按本轮失败处理，不伪造包装类型。
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
) -> LLMResponse:
    """编译面：带输出校验 + 自动重试的 LLM 调用。

    ``check(content) -> (ok, reason)``:
    校验 LLM 输出。ok=False 时，将 reason 追加到对话，让 LLM 修正后重试。

    三种重试条件（共享同一 attempts 预算，实现见 _retry_core）:
    1. LLM 调用异常（timeout / 网络错误）
    2. 空响应（reasoning 吃光预算等——不追加修正消息，只重发）
    3. check() 返回 ok=False

    **max_attempts 语义: 总尝试次数，不是"失败后重试次数"**——
    max_attempts=2 = 最多 2 次尝试 = 1 次重试机会（调用点按此理解传参）。

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

    Returns:
        最后一次 LLMResponse（即使最终仍未通过校验，check_ok
        字段携带校验结果，调用方据此处理）。

    Raises:
        RuntimeError: 所有尝试都是异常（无任何响应可返回）时；
        FatalError/CancelledError 不经耗尽包装、原样直抛。
    """
    # 生产 LLMClient 在工厂中注入 RootConfig.retry。保留这个回退，使只
    # 实现 async_invoke 的轻量测试替身和第三方适配器仍可复用重试包装器。
    retry_config = getattr(client, "retry_config", RetryConfig())
    # 用新局部名承接（而非回写声明为 int|None / float|None 的形参）——形参
    # 声明类型对 pyright 是粘性的，回写后读取仍是 Optional；新名被推断为具体
    # 数值，range/减法/_sleep_backoff 才不误判 None。语义与运行时无变化。
    attempts = max_attempts if max_attempts is not None else retry_config.llm_max_attempts
    delay = base_delay if base_delay is not None else retry_config.llm_base_delay_seconds
    msgs = list(messages)
    # 闭包格子：classify 记录每次"拿到响应"的尝试，供耗尽时决定
    # 返回最后的校验失败响应 vs 抛 RuntimeError（契约见 docstring）。
    last_response: list[LLMResponse] = []

    def _classify(response: LLMResponse, attempt_no: int) -> tuple[bool, str, str]:
        last_response.append(response)
        content = response.content

        if not content.strip():
            # 诊断: reasoning 模型思考吃掉全部预算时 content 留空
            # （finish_reason=length + reasoning_content 非空）
            rc_len = len(response.reasoning_content or "")
            logger.warning(
                "空响应 %d/%d (finish=%s, reasoning=%d chars)",
                attempt_no,
                attempts,
                response.finish_reason,
                rc_len,
            )
            # 必须填充 check_ok=False——check 回调没被调用，默认 True
            # 会让调用方漏过空内容（审计: 最后一次尝试空响应时
            # Searcher 静默降级 0 候选 / Analyzer 产出空分析）
            response.check_ok = False
            response.check_reason = (
                f"输出为空（finish_reason={response.finish_reason}）——"
                f"请输出完整内容，不要只输出思考。"
            )
            return False, "empty", response.check_reason

        if check is None:
            return True, "", ""

        ok, reason = check(content)
        # check 结果记录进 response——调用方不再重复调用 check
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
        # 修正消息只属于 check_failed 分支——空响应重发同一请求即可，
        # 追加"assistant 空消息 + 修正指令"反而污染对话（与旧实现一致）。
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
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                extra_body=extra_body,
            ),
            attempts=attempts,
            delay=delay,
            classify=_classify,
            on_rejected=_on_rejected,
        )
    except _Exhausted as ex:
        # 最后一次尝试是异常，但此前拿到过（校验失败的）响应 → 按契约
        # 返回该响应，调用方读 check_ok 处理。
        if last_response:
            return cast(LLMResponse, last_response[-1])
        # 全部尝试都是异常 → RuntimeError（ingest 边界包装成 IngestError）。
        # 消息格式按异常类型区分，与旧实现一致。
        exc = ex.original
        last_error = str(exc) if isinstance(exc, RetryableError) else f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"LLM 调用 {attempts} 次均失败 - {last_error}") from None
