from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from wiki_agent.agent.base import BaseAgent
from wiki_agent.agent.commands import CommandRouter, create_command_router
from wiki_agent.config import AgentConfig as AgentCfg
from wiki_agent.config import CompileConfig, RetryConfig
from wiki_agent.context import Consolidator, ContextBuilder, ContextGovernor
from wiki_agent.conversation import Message, Session, SessionManager, ThinkingSegment
from wiki_agent.errors import RetryableError
from wiki_agent.events import AgentHook, CompositeHook, RunContext
from wiki_agent.issues import IssueService
from wiki_agent.llm import LLMClient, retry_llm_call
from wiki_agent.log import begin_trace, emit_event, get_logger, span
from wiki_agent.memory import Dreamer, MemoryStore
from wiki_agent.tools import RecordCorrection, ToolRegistry

if TYPE_CHECKING:
    # 仅类型检查期引用：application 组装 agent，运行时 import 会形成循环依赖
    from wiki_agent.jobs.service import JobService

logger = get_logger("REACT_RUNNER")


def _elapsed_ms(started: float) -> int:
    """started（time.monotonic 时刻）到现在的毫秒数，至少 1。"""
    return max(1, round((time.monotonic() - started) * 1000))


def _turn_thinking(response) -> list[ThinkingSegment]:
    """提取 assistant 消息的 thinking 段。工具回合的 content 是过程文本，
    一并计入；最终回合的 content 是正文，不计入。"""
    segments: list[ThinkingSegment] = []
    if response.reasoning_content:
        segments.append(ThinkingSegment(kind="think", text=response.reasoning_content))
    if response.tool_calls and response.content and response.content.strip():
        segments.append(ThinkingSegment(kind="think", text=response.content.strip()))
    return segments


class ReActRunner:
    """ReAct 循环：governor → LLM → tools，重复直到终止。

    restore / build / save 由 ReActAgent 负责。
    """

    def __init__(self, agent: ReActAgent):
        self._agent = agent
        # 登记本 runner 创建的工具 Task；取消回合时逐个取消并等待。
        self._active_tasks: set[asyncio.Task] = set()

    async def run_loop(
        self,
        session: Session,
        messages: list[Message],
        stream: bool,
        run_ctx: RunContext,
    ) -> None:
        """执行 ReAct 循环。

        Args:
            session: 当前会话（token 统计写入对象）。
            messages: 工作副本消息列表——循环内追加 assistant/tool 消息。
            stream: 为 True 时使用流式调用。
            run_ctx: 回合级上下文（贯穿工具事件）。

        Returns:
            None。循环在无工具调用时自动结束。
        """
        for _ in range(self._agent.max_loop):
            runner_messages = self._agent.context_governor.prepare_for_llm(
                session=session,
                messages=copy.deepcopy(messages),
                agent_config=self._agent.agent_config,
            )

            if not stream:
                had_tools = await self._invoke(session, messages, runner_messages, run_ctx)
            else:
                had_tools = await self._stream(session, messages, runner_messages, run_ctx)

            if not had_tools:
                break

    async def _execute_tools(
        self,
        tool_calls: list,
        run_ctx: RunContext,
        assistant: Message,
    ) -> list[Message]:
        """并发执行工具调用。

        Args:
            tool_calls: LLM 返回的工具调用列表（含 name/id/arguments）。
            run_ctx: 回合上下文，工具事件与 tools_used 记录于此。
            assistant: 发起这批调用的 assistant 消息——每次调用的耗时与
                成败作为 tool 段记入其 thinking，历史重放时据此展示。

        Returns:
            tool role 消息列表（每条对应一次工具调用，失败时
            content 为错误文本）。
        """

        for tc in tool_calls:
            await self._agent.hooks.on_tool_call_start(
                context=run_ctx,
                tool_name=tc.name,
                tool_call_id=tc.id,
                arguments=tc.arguments,
            )

        async def _run_one(tc):
            started = time.monotonic()
            async with span("tool_call", tool=tc.name, tool_call_id=tc.id) as s:
                try:
                    result = await self._agent.tool_registry.execute(tc.name, params=tc.arguments)
                    await self._agent.hooks.on_tool_result(
                        context=run_ctx,
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        result=result,
                    )
                    run_ctx.tools_used.append(tc.name)
                    s.set_attr("result_len", len(str(result)))
                    return tc, result, None, _elapsed_ms(started)
                except Exception as exc:
                    await self._agent.hooks.on_tool_error(
                        context=run_ctx,
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        error=exc,
                    )
                    return tc, f"工具执行错误: {exc}", exc, _elapsed_ms(started)

        tasks = [
            asyncio.create_task(_run_one(tc), name=f"wiki-tool:{tc.name}:{tc.id}")
            for tc in tool_calls
        ]
        self._active_tasks.update(tasks)
        try:
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # 显式逐个 cancel，不依赖 gather 的传播行为
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._active_tasks.difference_update(tasks)

        tool_msgs: list[Message] = []
        for tc, result, exc, ms in results:
            assistant.thinking.append(
                ThinkingSegment(
                    kind="tool",
                    name=tc.name,
                    arguments=tc.arguments,
                    ms=ms,
                    error=exc is not None,
                )
            )
            tool_msgs.append(
                Message(
                    role="tool",
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=str(result),
                )
            )
        return tool_msgs

    async def cancel_active_tools(self) -> None:
        """取消并等待当前 runner 管理的所有工具 Task。"""
        tasks = list(self._active_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.difference_update(tasks)

    async def _invoke(
        self,
        session: Session,
        messages: list[Message],
        runner_messages: list[Message],
        run_ctx: RunContext,
    ) -> bool:
        """调用 LLM 并执行工具（非流式）。

        Args:
            session: 会话（usage 统计写入对象）。
            messages: 工作副本——追加 assistant 与 tool 消息。
            runner_messages: 实际发送给 LLM 的消息（governor 处理过）。
            run_ctx: 回合上下文。

        Returns:
            True 表示存在工具调用需继续循环；False 表示已出最终回答。
        """
        async with span("llm_call", model=self._agent.llm.model_id, stream=False) as s:
            # 瞬态失败退避重试统一走 retry_llm_call；非流式无增量输出，静默重试即可。
            response = await retry_llm_call(
                lambda: self._agent.llm.async_invoke(
                    runner_messages,
                    tools=self._agent.tool_registry.get_all_schema_openai(),
                    max_tokens=self._agent.agent_config.max_tokens,
                ),
                retry_config=self._agent.retry_config,
            )
            if response.usage:
                s.set_attr("tokens", response.usage)

        if response.usage:
            session.update_token_cost(
                response.usage["prompt"],
                response.usage["completion"],
                response.usage["total"],
            )

        assistant = Message(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
            thinking=_turn_thinking(response),
        )
        messages.append(assistant)

        if not response.tool_calls:
            if response.content:
                print(response.content)
            if response.finish_reason == "length":
                logger.warning(
                    "回答被 max_tokens 截断（finish=length, usage=%s, reasoning=%d chars）",
                    getattr(response, "usage", {}),
                    len(response.reasoning_content or ""),
                )
                print("\n_(回答被 token 上限截断——调大 AGENT_MAX_TOKENS 或让我继续)_")
            return False

        tool_msgs = await self._execute_tools(response.tool_calls, run_ctx, assistant)
        messages.extend(tool_msgs)

        if response.content:
            print(response.content)

        return True

    # 内部：流式调用

    async def _stream(
        self,
        session: Session,
        messages: list[Message],
        runner_messages: list[Message],
        run_ctx: RunContext,
    ) -> bool:
        """调用 LLM 并执行工具（流式）。

        流式增量经 on_stream_delta hook 事件发布，渲染层只依赖 hook。
        瞬态失败经 retry_llm_call 退避重试；重试会重放已生成增量，
        流式下可接受。重试提示经 on_retry 走 hook 事件，属于 UI 反馈，
        不放进通用重试。

        Args:
            session: 会话（usage 统计写入对象）。
            messages: 工作副本——追加 assistant 与 tool 消息。
            runner_messages: 实际发送给 LLM 的消息（governor 处理过）。
            run_ctx: 回合上下文。

        Returns:
            True 表示存在工具调用需继续循环；False 表示已出最终回答
            （空响应时经 hook 发送提示文本）。
        """

        async def on_retry(attempt: int, total: int, exc: BaseException) -> None:
            # 只对 RetryableError 提示；未知异常可能是代码 bug，提示等待网络会误导
            if isinstance(exc, RetryableError):
                await self._agent.hooks.on_stream_delta(
                    run_ctx, f"_(网络抖动——重试中 {attempt}/{total - 1})_"
                )

        # reasoning 分片实时转发；本回合首个分片前先触发 on_reasoning_start
        reasoning_started = False

        async def on_reasoning(chunk: str) -> None:
            nonlocal reasoning_started
            if not reasoning_started:
                reasoning_started = True
                await self._agent.hooks.on_reasoning_start(run_ctx)
            await self._agent.hooks.on_reasoning_delta(run_ctx, chunk)

        async with span("llm_call", model=self._agent.llm.model_id, stream=True) as s:
            response = await retry_llm_call(
                lambda: self._agent.llm.async_stream(
                    runner_messages,
                    tools=self._agent.tool_registry.get_all_schema_openai(),
                    max_tokens=self._agent.agent_config.max_tokens,
                    on_delta=lambda delta: self._agent.hooks.on_stream_delta(run_ctx, delta),
                    on_reasoning=on_reasoning,
                ),
                retry_config=self._agent.retry_config,
                on_retry=on_retry,
            )
            if response.usage:
                s.set_attr("tokens", response.usage)
            if not (response.content and response.content.strip()) and not response.tool_calls:
                s.set_attr("empty_response", True)
            # finish=length 表示生成被 max_tokens 截断（reasoning 模型
            # 思考占用预算后正文中断）——span 记录现场供诊断
            if response.finish_reason == "length":
                s.set_attr("truncated", True)
                s.set_attr("reasoning_len", len(response.reasoning_content or ""))

        if reasoning_started:
            await self._agent.hooks.on_reasoning_end(run_ctx)

        has_text = bool(response.content and response.content.strip())
        assistant = Message(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
            thinking=_turn_thinking(response),
        )
        messages.append(assistant)

        if response.usage:
            session.update_token_cost(
                response.usage["prompt"],
                response.usage["completion"],
                response.usage["total"],
            )

        if response.tool_calls:
            tool_msgs = await self._execute_tools(response.tool_calls, run_ctx, assistant)
            messages.extend(tool_msgs)
            return True
        else:
            if not has_text:
                logger.warning(
                    "LLM 返回空响应（无文本、无工具调用）。usage=%s finish=%s",
                    getattr(response, "usage", {}),
                    getattr(response, "finish_reason", "?"),
                )
                # 空响应需要让用户感知，提示经 hook 事件发出
                await self._agent.hooks.on_stream_delta(run_ctx, "_(模型未生成回答，请重试)_")
            elif response.finish_reason == "length":
                # 截断必须对用户可见，否则半截回答会被当作完整回答保存
                logger.warning(
                    "回答被 max_tokens 截断（finish=length, usage=%s, reasoning=%d chars）",
                    getattr(response, "usage", {}),
                    len(response.reasoning_content or ""),
                )
                await self._agent.hooks.on_stream_delta(
                    run_ctx, "\n\n_(回答被 token 上限截断——调大 AGENT_MAX_TOKENS 或让我继续)_"
                )
            return False


class ReActAgent(BaseAgent):
    SYSTEM_PROMPT = """
        # 用户画像
        {user_description}

        # 知识库环境
        {wiki_context}

        你背后是一个结构化的本地 wiki 知识库。用户问你问题时，你通过工具探索 wiki 来获取信息，而不是凭记忆回答。

        # 工作方式
        1. 面对知识性问题，先从全局入口入手（如 index 或目录），定位可能相关的页面
        2. 打开具体页面阅读内容，必要时沿着页面内的链接追踪关联页面
        3. 综合多页信息后给出回答，注明信息来源（页面路径或文件名）

        # 约束
        - wiki 知识库中的内容优先——你的已有知识只用于理解，不作为信息来源
        - 如果 wiki 中没有足够信息，如实告知，不要编造
        - 给出具体引用路径，让用户可以自行查阅
        - **用户指出 wiki 内容有误、过时或缺失时**（明确表达不满/纠正/补充缺失），
          调用 RecordCorrection 记录进待修清单——知识库靠用户反馈演化，
          不要放过这个信号。普通问答不算纠错，不要误报

        # 你能使用的工具
        {tools_description}

        # 最近的对话摘要
        {summary}

    """

    def __init__(
        self,
        name: str,
        llm: LLMClient,
        vlm: LLMClient,
        tool_registry: ToolRegistry,
        workspace: Path,
        issue_service: IssueService,
        session_manager: SessionManager | None = None,
        wiki_dir: str | Path | None = None,
        materials_dir: str | Path | None = None,
        hooks: list[AgentHook] | None = None,
        agent_config=None,
        compile_config: CompileConfig | None = None,
        retry_config: RetryConfig | None = None,
        job_service: JobService | None = None,
        commands: CommandRouter | None = None,
    ):
        super().__init__(name=name, workspace=workspace)
        # 依赖由调用方注入：job_service 可缺席（纯问答会话无任务队列），
        # issue_service 必备（RecordCorrection 工具依赖）。
        self.job_service = job_service
        self.llm = llm
        # wiki/materials 路径由调用方注入，命令层从这里读取
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else None
        self.materials_dir = Path(materials_dir) if materials_dir is not None else None
        # vlm 供编译类命令使用（CompilePipeline 需要）
        self.vlm = vlm
        self.agent_config = agent_config or AgentCfg()
        # 由组装入口传入 RootConfig.compile；默认值仅供单元测试和直接构造。
        self.compile_config = compile_config or CompileConfig()
        self.retry_config = retry_config or RetryConfig()
        # 会话管理器由调用方注入；缺席时用默认实现
        self.session_manager = session_manager or SessionManager(workspace=workspace)
        self.tool_registry = tool_registry
        self.memory_store = MemoryStore(workspace=workspace)
        self.issue_service = issue_service
        self.tool_registry.register(RecordCorrection(self.issue_service))
        self.context_builder = ContextBuilder(
            system_prompt=self.SYSTEM_PROMPT,
            tool_registry=tool_registry,
            memory_store=self.memory_store,
            issue_service=self.issue_service,
            # wiki 目录显式传入；build 时读取其中 purpose/schema/index 组装上下文
            wiki_dir=wiki_dir,
            agent_config=self.agent_config,
        )
        self.context_governor = ContextGovernor(workspace=workspace, agent_config=self.agent_config)
        self.consolidator = Consolidator(
            consolidate_ratio=self.agent_config.consolidate_ratio,
            trigger_ratio=self.agent_config.trigger_ratio,
        )
        # 命令 router 由调用方注入（内置命令 + application 层命令）；缺席用内置 router
        self.commands = commands or create_command_router()
        self.dreamer = Dreamer(workspace=workspace, memory_store=self.memory_store)
        self._dream_task: asyncio.Task | None = None
        self.max_loop = self.agent_config.max_loop

        _raw = hooks or []
        self.hooks: AgentHook = (
            CompositeHook(_raw) if len(_raw) > 1 else _raw[0] if _raw else AgentHook()
        )

        self._runner = ReActRunner(self)

    async def _run(
        self,
        session_key: str,
        user_input: str,
        stream=False,
        run_id: str | None = None,
    ):
        self._ensure_dream_task()
        # 锁覆盖整个 turn：两个 turn 并发会基于同一旧 history 生成回答并交错
        # 写回，覆盖 history/token cost/compaction 状态。
        async with self.session_manager.session_lock(session_key):
            begin_trace()
            async with span("turn", session=session_key):
                await self._run_turn(
                    session_key=session_key,
                    user_input=user_input,
                    stream=stream,
                    run_id=run_id,
                )

    @staticmethod
    def _snapshot_session(session: Session) -> dict:
        """保存本轮可能被 compaction 改写的 session 临时状态。"""
        return {
            "last_consolidated": session.last_consolidated,
            "last_summary": session.last_summary,
            "current_window_tokens": session.current_window_tokens,
            "token_cost": dict(session.token_cost),
            "updated_at": session.updated_at,
        }

    @staticmethod
    def _restore_session(session: Session, snapshot: dict) -> None:
        """取消时恢复未提交的本轮状态；历史消息此时尚未追加。"""
        session.last_consolidated = snapshot["last_consolidated"]
        session.last_summary = snapshot["last_summary"]
        session.current_window_tokens = snapshot["current_window_tokens"]
        # token_cost 是已实际发生的 LLM 消耗，取消不恢复；与本轮是否写入 history 无关
        session.updated_at = snapshot["updated_at"]

    async def _notify_cancelled(self, run_ctx: RunContext, reason: str) -> None:
        """取消路径的善后；清理失败不能掩盖原始取消。"""
        run_ctx.stop_reason = "cancelled"
        run_ctx.error = reason
        run_ctx.exception = asyncio.CancelledError(reason)
        try:
            await asyncio.shield(self._runner.cancel_active_tools())
            await asyncio.shield(self.hooks.on_run_error(run_ctx))
            # renderer 的 on_run_end 负责关闭未完成的 stream/tool UI，
            # 非正常结束也要执行其清理逻辑
            await asyncio.shield(self.hooks.on_run_end(run_ctx))
        except Exception as exc:
            logger.warning("取消收尾失败: %s: %s", type(exc).__name__, str(exc)[:160])

    TITLE_PROMPT = (
        "你在为一次本地知识库问答起标题。用一个不超过十个字的名词短语"
        "概括用户问题的主题，跟随用户提问的语言；不要引号、句号或前缀，"
        "只输出标题本身。"
    )

    async def generate_session_title(self, question: str, answer: str) -> str:
        """一次轻量非流式调用生成会话短标题。

        关闭思考、小 max_tokens，不进 ReAct 循环。调用失败抛异常，
        由编排方改用已保存的截断标题。

        Args:
            question: 用户首轮问题原文。
            answer: 助手首轮回答（截断后作为主题上下文）。

        Returns:
            模型产出的标题文本（未清洗）。
        """
        prompt = f"用户问题：{question.strip()}\n助手回答：{answer.strip()[:300]}"
        response = await self.llm.async_invoke(
            [
                Message(role="system", content=self.TITLE_PROMPT),
                Message(role="user", content=prompt),
            ],
            max_tokens=48,
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return (response.content or "").strip()

    async def _run_turn(
        self,
        *,
        session_key: str,
        user_input: str,
        stream: bool,
        run_id: str | None = None,
    ):
        """带取消事务边界的一轮 Agent 执行。"""
        run_ctx = RunContext(
            session_key=session_key,
            run_id=run_id or f"run_{uuid4().hex}",
        )
        session: Session = self.session_manager.get_or_create(session_key=session_key)
        # closed 只表示上一轮 idle 清理完成；用户重新输入时恢复活动态
        session.status = "active"
        session.updated_at = datetime.now().isoformat()
        snapshot = self._snapshot_session(session)
        turn_state = {"compaction_persisted": False}
        try:
            await self._run_turn_impl(
                session=session,
                user_input=user_input,
                stream=stream,
                run_ctx=run_ctx,
                turn_state=turn_state,
            )
        except asyncio.CancelledError as exc:
            usage = {
                key: session.token_cost.get(key, 0) - snapshot["token_cost"].get(key, 0)
                for key in ("prompt", "completion", "total")
            }
            if not turn_state["compaction_persisted"]:
                self._restore_session(session, snapshot)
            emit_event(
                "agent_turn_cancelled",
                session=session_key,
                reason=str(exc) or "task_cancelled",
                tools_used=list(run_ctx.tools_used),
                usage=usage,
            )
            await self._notify_cancelled(run_ctx, str(exc) or "task_cancelled")
            raise
        except Exception as exc:
            run_ctx.stop_reason = "error"
            run_ctx.error = str(exc)
            run_ctx.exception = exc
            try:
                await self.hooks.on_run_error(run_ctx)
                await self.hooks.on_run_end(run_ctx)
            finally:
                raise

    async def _run_turn_impl(
        self,
        *,
        session: Session,
        user_input: str,
        stream: bool,
        run_ctx: RunContext,
        turn_state: dict,
    ):
        """执行一轮完整对话：命令分发 → 压缩 → 回答 → 保存。

        Args:
            session: 当前会话。
            user_input: 用户输入文本。
            stream: 为 True 时使用流式调用。
            run_ctx: 回合上下文。
            turn_state: 本轮可变状态（compaction_persisted 标记）。
        """
        await self.hooks.on_run_start(run_ctx)

        # 命令在压缩之前分发：需要 session 状态，但不应触发 LLM 压缩
        cmd_result = await self.commands.dispatch(
            user_input.strip(), session, self, run_context=run_ctx
        )
        if cmd_result is not None:
            if cmd_result.text:
                # 命令输出同样经流式增量事件发布
                await self.hooks.on_stream_delta(run_ctx, cmd_result.text + "\n\n")
            if cmd_result.rerun_with:
                # /retry 类命令：替换 user_input 后继续走完整流程
                user_input = cmd_result.rerun_with
            else:
                # 命令路径不跑 LLM loop，run 结束事件在此发出
                await self.hooks.on_run_end(run_ctx)
                return

        await self.hooks.on_status(run_ctx, "compacting")
        async with span("compaction", session=session.key) as s:
            consolidation = await self.consolidator.maybe_consolidate(
                llm=self.llm,
                session=session,
                context_builder=self.context_builder,
                context_windows=self.agent_config.context_windows,
                max_tokens=self.agent_config.max_tokens,
                replay_max_messages=self.agent_config.max_messages_length,
            )
            consolidated = consolidation.changed
            s.set_attr("consolidated", consolidated)
            s.set_attr("last_consolidated", session.last_consolidated)
            s.set_attr("compaction_status", consolidation.status)
            s.set_attr("compaction_duration_ms", consolidation.duration_ms)
            s.set_attr("compaction_attempts", consolidation.attempts)
            s.set_attr("compaction_failures", consolidation.failure_count)
            s.set_attr(
                "compaction_tokens",
                {
                    "prompt": consolidation.prompt_tokens,
                    "completion": consolidation.completion_tokens,
                    "total": consolidation.total_tokens,
                },
            )

        if consolidated:
            saved = await self.session_manager.asave(session)
            if saved:
                # 压缩 checkpoint 成功后即使本轮回答随后取消也保留，
                # 避免下一轮重复压缩
                turn_state["compaction_persisted"] = True
            # 单用户模式：所有 session 共享同一份 history
            await asyncio.to_thread(
                self.memory_store.append_history, session=session, summary=session.last_summary
            )
            session.last_memory_archived = len(session.history)

        current_message = Message(role="user", content=user_input)

        # history 只含未压缩部分，比落盘的完整历史短
        history = session.get_history(max_messages_length=self.agent_config.max_messages_length)
        messages = self.context_builder.build_messages(
            session=session,
            current_message=current_message,
            history=history,
            last_summary=session.last_summary,
        )
        initail_message_count = len(messages)

        await self._runner.run_loop(session, messages, stream, run_ctx)

        get_skip_count = self._get_skip_count(
            initial_message_count=initail_message_count,
        )

        session.add_messages(messages[get_skip_count:])  # 纯内存操作，不必经线程
        await self.session_manager.asave(session)

        run_ctx.final_content = messages[-1].content if messages else ""
        await self.hooks.on_run_end(run_ctx)

    def _ensure_dream_task(self) -> None:
        """首次运行时启动 idle 收尾与 Dream 后台任务。"""
        if self._dream_task is None or self._dream_task.done():
            self._dream_task = asyncio.create_task(
                self.dream_loop(self.agent_config.dream_poll_interval),
                name="wiki-agent-dream",
            )

    async def _finalize_idle_sessions(self) -> bool:
        """结束空闲会话，压缩旧消息并写入 Dream 输入。"""
        changed = False
        now = datetime.now()
        idle_seconds = self.agent_config.session_idle_minutes * 60
        tail_messages = self.agent_config.session_tail_messages
        for session in self.session_manager.cached_sessions():
            if session.status != "active":
                continue
            try:
                idle_for = (now - datetime.fromisoformat(session.updated_at)).total_seconds()
            except (TypeError, ValueError):
                continue
            if idle_for < idle_seconds:
                continue

            async with self.session_manager.session_lock(session.key):
                # 等锁期间可能已有新消息，取锁后重新检查再决定是否收尾
                try:
                    idle_for = (
                        datetime.now() - datetime.fromisoformat(session.updated_at)
                    ).total_seconds()
                except (TypeError, ValueError):
                    continue
                if session.status != "active" or idle_for < idle_seconds:
                    continue

                result = await self.consolidator.finalize_idle(
                    llm=self.llm,
                    session=session,
                    context_windows=self.agent_config.context_windows,
                    max_tokens=self.agent_config.max_tokens,
                    tail_messages=tail_messages,
                )
                if result.changed:
                    summary = result.summary or ""
                    boundary = (
                        result.boundary
                        if result.boundary is not None
                        else session.last_consolidated
                    )
                    session.last_summary = summary
                    session.last_consolidated = boundary
                    session.last_memory_archived = len(session.history)
                    await asyncio.to_thread(
                        self.memory_store.append_history,
                        session=session,
                        summary=summary,
                    )
                    changed = True
                    emit_event(
                        "session_idle_compacted",
                        session=session.key,
                        retained_messages=len(session.history) - boundary,
                    )
                # 即使没有新增摘要，也要结束 idle 状态，避免每轮重复检查。
                session.status = "closed"
                await self.session_manager.asave(session)
        return changed

    async def dream_loop(self, interval: int = 60):
        """定期结束 idle session，并处理待整理的 Dream 输入。

        Args:
            interval: 检查间隔（秒）。
        """
        while True:
            await asyncio.sleep(interval)
            try:
                await self._finalize_idle_sessions()
                if self.memory_store.get_unprocessed_history():
                    await self.dreamer.dream(llm=self.llm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Dream 是后台任务，失败不影响 agent 运行；下个周期重试，
                # 且 Dreamer 失败时不推进游标
                logger.warning(
                    "idle/dream 后台任务失败: %s: %s", type(exc).__name__, str(exc)[:200]
                )

    def _get_skip_count(self, initial_message_count: int) -> int:
        """计算本次新增消息在 messages 中的起始下标。

        Args:
            initial_message_count: build 后 messages 的初始长度。

        Returns:
            新消息的起始下标（丢弃 build 阶段的历史部分，
            只追加本轮新增消息）。
        """
        return initial_message_count - 1
