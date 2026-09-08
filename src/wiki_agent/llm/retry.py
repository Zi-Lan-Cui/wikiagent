"""LLM 调用包装——输出校验 + 自动重试。

作为中间件运行在 LLMClient 之上，不修改其代码。
校验失败时自动追加修正消息，让 LLM 重新输出。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from openai.types.chat import ChatCompletionToolParam

from wiki_agent.config import RetryConfig
from wiki_agent.conversation import LLMResponse, Message
from wiki_agent.errors import FatalError, RetryableError
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import get_logger, span

logger = get_logger("LLM_RETRY")

# ── check 回调签名 ────────────────────────────────────────
# (content: str) -> (ok: bool, reason: str)
OutputCheck = Callable[[str], tuple[bool, str]]


async def _sleep_backoff(attempt: int, base_delay: float) -> None:
    """指数退避睡眠。

    延迟 = 2^attempt * base_delay（三处重试共用）。

    Args:
        attempt: 已失败的尝试次数（从 0 起）。
        base_delay: 基础延迟（秒）。
    """
    await asyncio.sleep(base_delay * (2**attempt))


async def async_invoke_with_retry(
    client: LLMClient,
    messages: list[Message],
    *,
    check: OutputCheck | None = None,
    tools: list[ChatCompletionToolParam] | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.5,
    extra_body: dict | None = None,
    max_retries: int | None = None,
    base_delay: float | None = None,
) -> LLMResponse:
    """带输出校验 + 自动重试的 LLM 调用。

    ``check(content) -> (ok, reason)``:
    校验 LLM 输出。ok=False 时，将 reason 追加到对话，让 LLM 修正后重试。

    两种重试条件:
    1. LLM 调用异常（timeout / 网络错误）
    2. check() 返回 ok=False

    **max_retries 语义: 总尝试次数，不是"失败后重试次数"**——
    max_retries=2 = 最多 2 次尝试 = 1 次重试机会（调用点 stages.py
    按此理解传参）。

    Args:
        client: LLM 客户端。
        messages: 消息列表（校验失败时内部追加修正消息，不修改入参）。
        check: 输出校验回调 ``(content) -> (ok, reason)``。
        tools: OpenAI 工具 schema 列表。
        max_tokens: 生成 token 上限。
        temperature: 采样温度。
        extra_body: 附加请求体参数。
        max_retries: 总尝试次数（不是重试次数）。
        base_delay: 退避基础延迟（秒）。

    Returns:
        最后一次 LLMResponse（即使最终仍未通过校验，check_ok
        字段携带校验结果，调用方据此处理）。
    """
    # 生产 LLMClient 在工厂中注入 RootConfig.retry。保留这个回退，使只
    # 实现 async_invoke 的轻量测试替身和第三方适配器仍可复用重试包装器。
    retry_config = getattr(client, "retry_config", RetryConfig())
    # 用新局部名承接（而非回写声明为 int|None / float|None 的形参）——形参
    # 声明类型对 pyright 是粘性的，回写后读取仍是 Optional；新名被推断为具体
    # 数值，range/减法/_sleep_backoff 才不误判 None。语义与运行时无变化。
    attempts = max_retries if max_retries is not None else retry_config.llm_max_attempts
    delay = base_delay if base_delay is not None else retry_config.llm_base_delay_seconds
    msgs = list(messages)
    last_response: LLMResponse | None = None
    last_error: str | None = None

    for attempt in range(attempts):
        async with span("llm_attempt", attempt=attempt + 1, max_retries=attempts) as s:
            try:
                response = await client.async_invoke(
                    msgs,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=extra_body,
                )
            except asyncio.CancelledError:
                # 用户/上层任务主动取消，不是 provider 瞬态故障。
                # 不 sleep、不重试、不伪装成普通 LLM 错误，直接传播。
                s.mark_failure("cancelled")
                raise
            except FatalError as exc:
                # 401/404/400——重试一万次也没用，直接冒泡让边界处理
                logger.error("Fatal 错误，放弃重试: %s", exc)
                s.mark_failure("fatal")
                raise
            except RetryableError as exc:
                # 限流/超时/网络抖动——指数退避重试
                last_error = str(exc)
                logger.warning("调用失败 %d/%d: %s", attempt + 1, attempts, last_error)
                s.mark_failure("retryable")
                if attempt < attempts - 1:
                    await _sleep_backoff(attempt, delay)
                continue
            except Exception as exc:
                # 翻译漏网的防御兜底（理论不可达: client.async_invoke
                # 所有异常都经 translate_openai_error 翻译成 Fatal/Retryable，
                # 上面的分支已接住）。
                # 漏网异常大概率是"新 SDK 异常类型"而非代码 bug——
                # LLM 调用场景网络抖动概率远高于翻译函数出错，保守重试；
                # 若是 bug，重试两次后照样抛出，无害。
                # 与 translate_generic_error 的"未知默认 Fatal"不冲突:
                # 那是本地 IO/工具语境（重试可能放大问题），这是网络语境。
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("未知异常 %d/%d: %s", attempt + 1, attempts, last_error)
                s.mark_failure("unknown")
                if attempt < attempts - 1:
                    await _sleep_backoff(attempt, delay)
                continue

            last_response = response
            content = response.content

            if not content.strip():
                # 诊断: reasoning 模型思考吃掉全部预算时 content 留空
                # （finish_reason=length + reasoning_content 非空）
                rc_len = len(response.reasoning_content or "")
                logger.warning(
                    "空响应 %d/%d (finish=%s, reasoning=%d chars)",
                    attempt + 1,
                    attempts,
                    response.finish_reason,
                    rc_len,
                )
                s.mark_failure("empty")
                s.set_attr("finish_reason", response.finish_reason)
                # 必须填充 check_ok=False——check 回调没被调用，默认 True
                # 会让调用方漏过空内容（审计: 最后一次尝试空响应时
                # Searcher 静默降级 0 候选 / Analyzer 产出空分析）
                response.check_ok = False
                response.check_reason = (
                    f"输出为空（finish_reason={response.finish_reason}）——"
                    f"请输出完整内容，不要只输出思考。"
                )
                if attempt < attempts - 1:
                    await _sleep_backoff(attempt, delay)
                continue

            if check is None:
                return response

            ok, reason = check(content)
            # check 结果记录进 response——调用方不再重复调用 check
            response.check_ok = ok
            response.check_reason = "" if ok else reason
            if ok:
                return response

            last_error = reason
            # I5 排查观测——截断之谜需要 finish_reason + usage 收集案例
            # （380-480 字符处截断，max_tokens 远未达、thinking 已关）。
            # cache_hit/cache_miss 顺带记录——prompt cache 命中率监控，
            # 命中率骤降是有人破坏前缀稳定的第一信号。
            usage = response.usage or {}
            logger.warning(
                "校验失败 %d/%d: %s（finish=%s, content_len=%d, "
                "completion_tokens=%s, prompt_tokens=%s, "
                "cache_hit=%s, cache_miss=%s）",
                attempt + 1,
                attempts,
                reason[:120],
                response.finish_reason,
                len(content),
                usage.get("completion"),
                usage.get("prompt"),
                usage.get("cache_hit"),
                usage.get("cache_miss"),
            )
            s.mark_failure("check_failed")
            s.set_attr("check_reason", reason[:120])
            s.set_attr("finish_reason", response.finish_reason)
            s.set_attr("content_len", len(content))
            if attempt < attempts - 1:
                msgs.append(Message(role="assistant", content=content))
                msgs.append(
                    Message(
                        role="user",
                        content=(f"上一次输出有问题，请修正后重新输出。问题是: {reason}"),
                    )
                )
                await _sleep_backoff(attempt, delay)

    if last_response is not None:
        return last_response
    raise RuntimeError(f"LLM 调用 {attempts} 次均失败 - {last_error}")
