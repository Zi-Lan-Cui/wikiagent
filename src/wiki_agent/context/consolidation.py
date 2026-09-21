import time
from dataclasses import dataclass, field

from wiki_agent.context.context_builder import ContextBuilder
from wiki_agent.conversation import Message, Session
from wiki_agent.llm import LLMClient
from wiki_agent.log import emit_event, get_logger
from wiki_agent.utils import estimate_text_tokens, truncate_text_by_tokens

logger = get_logger("CONSOLIDATOR")


@dataclass
class ArchiveResult:
    """一次摘要 LLM 调用的诊断结果。"""

    summary: str | None = None
    duration_ms: float = 0.0
    input_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    failure_reason: str = ""

    @property
    def succeeded(self) -> bool:
        return bool(self.summary) and not self.failure_reason


@dataclass
class ConsolidationResult:
    """一次压缩流程的汇总结果。

    调用方通过 ``changed/status`` 判断结果，并从字段读取诊断指标。
    """

    changed: bool = False
    status: str = "not_needed"  # not_needed/succeeded/partial/failed
    summary: str | None = None
    boundary: int | None = None
    old_boundary: int = 0
    estimated_tokens: int = 0
    duration_ms: float = 0.0
    attempts: int = 0
    failure_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    failure_reasons: list[str] = field(default_factory=list)

    def add_archive(self, result: ArchiveResult) -> None:
        self.attempts += 1
        self.duration_ms += result.duration_ms
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.total_tokens += result.total_tokens
        self.cache_hit += result.cache_hit
        self.cache_miss += result.cache_miss
        if not result.succeeded:
            self.failure_count += 1
            if result.failure_reason:
                self.failure_reasons.append(result.failure_reason)


class Consolidator:
    """
    该模块负责使用LLM的压缩逻辑的实现，该模块只负责生产压缩结果，不负责将结果写入任何地方
    """

    _SAFETY_BUFFER = 1024  # 留给token估计错误的安全余量
    _MAX_CONSOLIDATE_LOOP = 5

    _CONSOLIDATOR_PROMPT = """
    你现在是一位出色的语言大师，擅长在不大量损失上下文信息的同时将复杂的语言压缩到极致。你现在要帮忙压缩的文本如下:
    {text}

    # 同对话的上一次压缩结果(可能为空)：
    {last_summary}

    ## 重要事项：
    在不影响主要语义表达的前提下，越短越好
    """

    def __init__(self, consolidate_ratio: float = 0.5, trigger_ratio: float = 0.8):
        self._consolidate_ratio = consolidate_ratio
        self._trigger_ratio = trigger_ratio

    def _input_token_budget(self, context_windows, max_tokens):
        return context_windows - max_tokens - self._SAFETY_BUFFER

    def _maybe_truncate(self, text: str, budget: int) -> str:
        if budget <= 0:
            return ""

        return truncate_text_by_tokens(text, budget)

    async def archive(
        self,
        llm: LLMClient,
        messages: list[Message],
        context_windows: int,
        max_tokens: int,
        last_summary: str = "",
    ) -> ArchiveResult:
        """
        压缩消息列表为一段摘要。

        Args:
            llm: LLM 客户端。
            messages: 待压缩的消息列表（按逆序拼接——截断时
                保留最近信息）。
            context_windows: 模型上下文窗口大小。
            max_tokens: 摘要的最大生成 token 数。
            last_summary: 上一次压缩摘要（有剩余预算时并入）。

        Returns:
            单次调用的摘要和诊断指标；输入为空或调用失败时
            ``succeeded`` 为 False。
        """
        started = time.perf_counter()
        result = ArchiveResult()
        if not messages:
            result.failure_reason = "empty_messages"
            return result

        # 输入进LLM之前，要确保内容合法，确保内容在上下文窗口之内，如果超出
        # 需要用轻量化的方式截取，不宜堆叠调用LLM
        try:
            # 保持消息为基本单元不好进行截断，所以要先进行转换，把消息转换为文本
            # 这里采用逆序，避免截断时丢失最近的信息
            text = "\n".join([m.text_schema for m in reversed(messages)])
            budget = self._input_token_budget(context_windows, max_tokens)
            truncate_text = self._maybe_truncate(text, budget)

            # 当上下文有剩余时加入已有摘要；没有空余则跳过。
            truncated_summary = None
            if last_summary and truncate_text:
                text_cost = estimate_text_tokens(truncate_text)
                truncated_summary = self._maybe_truncate(last_summary, budget - text_cost)

            if truncate_text:
                result.input_tokens = estimate_text_tokens(truncate_text)
                need_consolidate_messages = [
                    Message(
                        role="system",
                        content=self._CONSOLIDATOR_PROMPT.format(
                            text=truncate_text, last_summary=truncated_summary or ""
                        ),
                    )
                ]

                response = await llm.async_invoke(need_consolidate_messages, max_tokens=max_tokens)
                summary = response.content

                usage = response.usage or {}
                result.prompt_tokens = int(usage.get("prompt", 0) or 0)
                result.completion_tokens = int(usage.get("completion", 0) or 0)
                result.total_tokens = int(usage.get("total", 0) or 0)
                result.cache_hit = int(usage.get("cache_hit", 0) or 0)
                result.cache_miss = int(usage.get("cache_miss", 0) or 0)

                if response.finish_reason == "error":
                    raise RuntimeError(f"LLM returned error: {response.content}")

                result.summary = summary
                return result
            result.failure_reason = "empty_truncated_input"
        except Exception as e:
            result.failure_reason = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning("压缩失败 - %s: %s", type(e).__name__, str(e)[:200])
        finally:
            result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    async def _consolidate_replay_overflow(
        self,
        llm: LLMClient,
        messages: list[Message],
        context_windows: int,
        max_tokens: int,
        last_consolidate: int,
        replay_max_messages: int,
        last_summary: str = "",
    ) -> tuple[ArchiveResult, int] | None:
        """
        判断并压缩窗口外溢出的未压缩历史。

        Args:
            llm: LLM 客户端。
            messages: 完整历史消息。
            context_windows: 模型上下文窗口大小。
            max_tokens: 摘要的最大生成 token 数。
            last_consolidate: 上次压缩到的下标。
            replay_max_messages: 保留的最远消息数（replay 窗口）。
            last_summary: 上一次压缩摘要。

        Returns:
            ``(ArchiveResult, end_idx)``；无需压缩时返回 None。
        """
        if len(messages) - last_consolidate <= replay_max_messages:
            return None

        # 找到压缩的end_idx，这里不需要保证要压缩的会话对api合法，也不需要保证剩余对api合法
        # build阶段会在剩下的replay_max_messages消息中寻找合法部分
        end_idx = len(messages) - replay_max_messages
        need_cosolidate_meesages = messages[last_consolidate:end_idx]

        archive_result = await self.archive(
            llm=llm,
            messages=need_cosolidate_meesages,
            context_windows=context_windows,
            max_tokens=max_tokens,
            last_summary=last_summary,
        )
        return archive_result, end_idx

    def _estimate_session_prompt_tokens(
        self, session: Session, context_builder: ContextBuilder, replay_max_messages: int
    ) -> int:
        # 只计算窗口内的元素
        if replay_max_messages > 0:
            # 只取窗口的未压缩历史，保证与外部创建的inital_message获取一致
            unconsolidate_history = session.get_history(max_messages_length=replay_max_messages)
        else:
            unconsolidate_history = []

        last_summary = session.last_summary
        initial_messages = context_builder.build_messages(
            session=session,
            history=unconsolidate_history,
            # 传递current_message 过于麻烦且破坏结构，这里使用占位符替代
            current_message=Message(role="user", content="[token probe]"),
            last_summary=last_summary,
        )

        initial_message_text = "\n".join([message.text_schema for message in initial_messages])
        return estimate_text_tokens(initial_message_text)

    def _pick_consolidation_boundry_by_tokens(
        self, session: Session, tokens_to_remove: int
    ) -> int | None:
        """寻找满足压缩 token 目标的最小合法切分边界。

        从最早未压缩的消息往最近遍历；切分点落在 user 消息上
        （避免切开工具调用-结果配对）。

        Args:
            session: 会话（读未压缩历史）。
            tokens_to_remove: 需要移除的 token 数。

        Returns:
            边界下标（切片 end）；无合法边界（含无内容可压）
            时返回 None。
        """
        start = session.last_consolidated

        # 没有可压缩的，包括tokens不对和已经全被压缩两种情况
        if tokens_to_remove <= 0 or start >= len(session.history):
            return None

        removed_tokens = 0
        for idx in range(start, len(session.history)):
            message_tokens = estimate_text_tokens(session.history[idx].text_schema)
            removed_tokens += message_tokens
            # 切分点在user上，避免切开工具调用
            if session.history[idx].role == "user" and removed_tokens >= tokens_to_remove:
                return idx + 1

        return None

    async def maybe_consolidate(
        self,
        llm: LLMClient,
        session: Session,
        context_builder: ContextBuilder,
        context_windows: int,
        max_tokens: int,
        replay_max_messages: int,
    ) -> ConsolidationResult:
        """
        按需压缩会话历史——内部更新 session 压缩状态，但不保存。

        两种策略:
        1. 压缩超出 get_history 提取窗口（对 LLM 已不可见）的内容。
        2. 窗口内总消息 token 超出预算时，对窗口内未压缩消息
           整体压缩。

        Args:
            llm: LLM 客户端。
            session: 目标会话（last_consolidated/last_summary 会被推进）。
            context_builder: 用于估计窗口 prompt token 数。
            context_windows: 模型上下文窗口大小。
            max_tokens: 摘要的最大生成 token 数。
            replay_max_messages: 保留的最远消息数。

        Returns:
            结构化压缩结果，包含边界、耗时、token 和失败指标。
        """
        started = time.perf_counter()
        old_consolidated = session.last_consolidated
        metrics = ConsolidationResult(old_boundary=old_consolidated)

        if not session.history:
            metrics.duration_ms = (time.perf_counter() - started) * 1000
            return metrics

        budget = self._input_token_budget(context_windows, max_tokens)
        if budget <= 0:
            metrics.status = "failed"
            metrics.failure_count = 1
            metrics.failure_reasons.append("invalid_input_budget")
            metrics.duration_ms = (time.perf_counter() - started) * 1000
            return metrics

        # 压缩掉超出窗口的未压缩消息，这些消息对LLM已经不可见
        # 不应该把replay_max_messages设置过小，这个不应该被频繁触发
        # 否则压缩一条又来一条
        # 这个裁剪没有考虑是否能够合法的截断，因为这些消息本身也不会再被传给api
        # 但是被截断后的replay窗口内的消息，应该保持获取时的合法性
        # archive 内部有 truncate 兜底——超预算输入被截断后调用仍合法
        result = await self._consolidate_replay_overflow(
            llm=llm,
            messages=session.history,
            context_windows=context_windows,
            max_tokens=max_tokens,
            last_consolidate=session.last_consolidated,
            last_summary=session.last_summary,
            replay_max_messages=replay_max_messages,
        )

        compaction_failed = False
        if result:
            archive_result, end_idx = result
            metrics.add_archive(archive_result)
            if archive_result.succeeded:
                session.last_summary = archive_result.summary or ""
                session.last_consolidated = end_idx
            else:
                compaction_failed = True
                logger.warning(
                    "strategy 1 压缩失败——保留 last_consolidated=%d，交给 Governor 截断",
                    session.last_consolidated,
                )
        try:
            # 尝试将整个窗口内未压缩的历史消息都放入messages，如果超出上下文窗口，对窗口内未压缩的消息进行压缩
            # 这里和nanobot靠上面的压缩来获取所有窗口内未压缩历史不同，这里直接传递真实窗口长度
            # 避免取的窗口和外部真实窗口不一致，导致估计错误
            # 这个内部的截断位置，是会被传递给api的，所以这里的截断应该慎重考虑位置
            # 或者让get_history的时候保证开始是合法的
            estimate_tokens = self._estimate_session_prompt_tokens(
                session=session,
                context_builder=context_builder,
                replay_max_messages=replay_max_messages,
            )

        except Exception as e:
            logger.warning(
                f" {session.key} token 估计失败 - %s: %s，将按0处理", type(e).__name__, str(e)[:150]
            )
            estimate_tokens = 0

        trigger_tokens = budget * self._trigger_ratio
        target_tokens = budget * self._consolidate_ratio
        if estimate_tokens >= trigger_tokens and not compaction_failed:
            for _ in range(self._MAX_CONSOLIDATE_LOOP):
                if estimate_tokens < target_tokens:
                    break

                boundary = self._pick_consolidation_boundry_by_tokens(
                    session=session, tokens_to_remove=int(max(1, estimate_tokens - target_tokens))
                )

                if not boundary:
                    logger.warning("没有有效的压缩区间,将停止压缩，已压缩部分已保存至会话")
                    break

                consolidate_chunk = session.history[session.last_consolidated : boundary]

                archive_result = await self.archive(
                    llm=llm,
                    messages=consolidate_chunk,
                    context_windows=context_windows,
                    max_tokens=max_tokens,
                    last_summary=session.last_summary,
                )

                metrics.add_archive(archive_result)
                if archive_result.succeeded:
                    session.last_summary = archive_result.summary or ""
                    session.last_consolidated = boundary
                else:
                    logger.warning(
                        "窗口压缩失败——保留 last_consolidated=%d，交给 Governor 截断",
                        session.last_consolidated,
                    )
                    break

                try:
                    estimate_tokens = self._estimate_session_prompt_tokens(
                        session=session,
                        context_builder=context_builder,
                        replay_max_messages=replay_max_messages,
                    )
                except Exception as e:
                    logger.warning(
                        f"{session.key}估计tokens失败 - %s: %s，将按0处理",
                        type(e).__name__,
                        str(e)[:150],
                    )
                    estimate_tokens = 0

            if estimate_tokens >= target_tokens:
                logger.warning("压缩后仍然超出预算")

        # 更新session的窗口token
        session.current_window_tokens = estimate_tokens
        metrics.changed = session.last_consolidated > old_consolidated
        metrics.boundary = session.last_consolidated
        metrics.summary = session.last_summary if metrics.changed else None
        metrics.estimated_tokens = estimate_tokens
        metrics.status = (
            "partial"
            if metrics.changed and metrics.failure_count
            else "succeeded"
            if metrics.changed
            else "failed"
            if metrics.failure_count
            else "not_needed"
        )
        metrics.duration_ms = (time.perf_counter() - started) * 1000
        emit_event(
            "compaction_result",
            session=session.key,
            status=metrics.status,
            changed=metrics.changed,
            duration_ms=round(metrics.duration_ms, 2),
            attempts=metrics.attempts,
            failure_count=metrics.failure_count,
            tokens={
                "prompt": metrics.prompt_tokens,
                "completion": metrics.completion_tokens,
                "total": metrics.total_tokens,
                "cache_hit": metrics.cache_hit,
                "cache_miss": metrics.cache_miss,
            },
            old_boundary=metrics.old_boundary,
            boundary=metrics.boundary,
            estimated_tokens=metrics.estimated_tokens,
            failure_reasons=metrics.failure_reasons,
        )
        return metrics

    async def finalize_idle(
        self,
        llm: LLMClient,
        session: Session,
        context_windows: int,
        max_tokens: int,
        tail_messages: int = 6,
    ) -> ConsolidationResult:
        """结束 idle session：生成收尾摘要并返回新的压缩边界。

        摘要输入从 ``last_memory_archived`` 开始，保证每批会话内容只
        进入 Dreamer 一次；返回边界保留最近 ``tail_messages`` 条消息。
        """
        start = min(session.last_memory_archived, len(session.history))
        started = time.perf_counter()
        metrics = ConsolidationResult(old_boundary=session.last_consolidated)
        if start >= len(session.history):
            metrics.duration_ms = (time.perf_counter() - started) * 1000
            return metrics
        archive_result = await self.archive(
            llm=llm,
            messages=session.history[start:],
            context_windows=context_windows,
            max_tokens=max_tokens,
            last_summary=session.last_summary,
        )
        metrics.add_archive(archive_result)
        if not archive_result.succeeded:
            metrics.status = "failed"
            metrics.failure_reasons.append(archive_result.failure_reason)
            metrics.duration_ms = (time.perf_counter() - started) * 1000
            emit_event(
                "compaction_result",
                session=session.key,
                status=metrics.status,
                changed=False,
                duration_ms=round(metrics.duration_ms, 2),
                attempts=metrics.attempts,
                failure_count=metrics.failure_count,
                failure_reasons=metrics.failure_reasons,
            )
            return metrics
        boundary = max(session.last_consolidated, len(session.history) - max(0, tail_messages))
        metrics.changed = True
        metrics.status = "succeeded"
        metrics.summary = archive_result.summary
        metrics.boundary = boundary
        metrics.duration_ms = (time.perf_counter() - started) * 1000
        emit_event(
            "compaction_result",
            session=session.key,
            status=metrics.status,
            changed=True,
            duration_ms=round(metrics.duration_ms, 2),
            attempts=metrics.attempts,
            failure_count=metrics.failure_count,
            tokens={
                "prompt": metrics.prompt_tokens,
                "completion": metrics.completion_tokens,
                "total": metrics.total_tokens,
            },
            old_boundary=metrics.old_boundary,
            boundary=boundary,
        )
        return metrics
