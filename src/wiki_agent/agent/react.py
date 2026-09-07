from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from openai.types.chat import ChatCompletionToolParam

from wiki_agent.agent.base import BaseAgent
from wiki_agent.command import create_command_router
from wiki_agent.config import AgentConfig as AgentCfg
from wiki_agent.config import CompileConfig, RetryConfig
from wiki_agent.consolidator import Consolidator
from wiki_agent.context import ContextBuilder, ContextGovernor
from wiki_agent.errors import RetryableError
from wiki_agent.hook import AgentHook, CompositeHook, RunContext
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.llm import LLMClient
from wiki_agent.log import begin_trace, emit_event, get_logger, span
from wiki_agent.memory import Dreamer, MemoryStore
from wiki_agent.message import LLMResponse, Message
from wiki_agent.session import Session, SessionManager
from wiki_agent.tools import RecordCorrection, ToolRegistry

# ════════════════════════════════════════════════════════════════
#  ReActRunner — 执行引擎（只跑 ReAct loop）
# ════════════════════════════════════════════════════════════════
logger = get_logger("REACT_RUNNER")


class ReActRunner:
    """只负责 ReAct 循环：governor → LLM → tools，重复直到终止。

    restore / build / save 留在 ReActAgent 中，属于状态机流转。
    """

    def __init__(self, agent: ReActAgent):
        self._agent = agent
        # 所有主动创建的工具 Task 都登记在这里；取消 Agent 回合时统一收尾。
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

    # ── 工具执行 ─────────────────────────────────────

    async def _execute_tools(
        self,
        tool_calls: list,
        run_ctx: RunContext,
    ) -> list[Message]:
        """并发执行工具调用。

        Args:
            tool_calls: LLM 返回的工具调用列表（含 name/id/arguments）。
            run_ctx: 回合上下文——工具事件与 tools_used 记录对象。

        Returns:
            tool role 消息列表（每条对应一次工具调用，失败时
            content 为错误文本）。
        """

        for tc in tool_calls:
            await self._agent._hooks.on_tool_call_start(
                context=run_ctx,
                tool_name=tc.name,
                tool_call_id=tc.id,
                arguments=tc.arguments,
            )

        async def _run_one(tc):
            async with span("tool_call", tool=tc.name, tool_call_id=tc.id) as s:
                try:
                    result = await self._agent.tool_registry.execute(tc.name, params=tc.arguments)
                    await self._agent._hooks.on_tool_result(
                        context=run_ctx,
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        result=result,
                    )
                    run_ctx.tools_used.append(tc.name)
                    s.set_attr("result_len", len(str(result)))
                    return tc, result, None
                except Exception as exc:
                    await self._agent._hooks.on_tool_error(
                        context=run_ctx,
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        error=exc,
                    )
                    return tc, f"工具执行错误: {exc}", exc

        tasks = [
            asyncio.create_task(_run_one(tc), name=f"wiki-tool:{tc.name}:{tc.id}")
            for tc in tool_calls
        ]
        self._active_tasks.update(tasks)
        try:
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # gather 通常会传播取消，但显式逐个 cancel 是保护性兜底，
            # 尤其防止未来改成 shield/独立等待后留下后台工具。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._active_tasks.difference_update(tasks)

        tool_msgs: list[Message] = []
        for tc, result, _exc in results:
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

    # ── 内部：LLM 调用（带重试）───────────────────────

    async def _invoke_with_retry(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam],
        *,
        max_tokens: int,
        run_ctx: RunContext,
        on_delta: Callable | None = None,
    ) -> LLMResponse:
        """LLM 调用包装——RetryableError 指数退避重试。

        流式/非流式共用。流式重试时 on_delta 回调会重复收到已
        生成增量——流式本就向前滚动，多刷一段可接受；非流式无回调。

        Args:
            messages: 发送给 LLM 的消息。
            tools: OpenAI 工具 schema 列表。
            max_tokens: 生成 token 上限。
            run_ctx: 回合上下文（重试提示经 hook 事件显示）。
            on_delta: 流式增量回调（流式路径传入）。

        Returns:
            最后一次成功的 LLMResponse。

        Raises:
            RetryableError: 重试耗尽后原样抛给上层（本轮失败，
                CLI 边界处理，不会崩掉交互循环）。
        """
        retry = self._agent.retry_config
        max_attempts = retry.llm_max_attempts
        delay = retry.llm_base_delay_seconds
        for attempt in range(max_attempts):
            try:
                if on_delta is not None:
                    return await self._agent.llm.async_stream(
                        messages, tools=tools, max_tokens=max_tokens, on_delta=on_delta
                    )
                return await self._agent.llm.async_invoke(
                    messages, tools=tools, max_tokens=max_tokens
                )
            except RetryableError:
                # 流式退避会让用户看到停顿——提示等待（非流式
                # 屏幕无动态，静默退避即可）
                if on_delta is not None and attempt < max_attempts - 1:
                    await self._agent._hooks.on_stream_delta(
                        run_ctx, f"_(网络抖动——重试中 {attempt + 1}/{max_attempts - 1})_"
                    )
                if attempt < max_attempts - 1:
                    logger.warning(
                        "LLM 调用失败 %d/%d（%s），%.1fs 后重试",
                        attempt + 1,
                        max_attempts,
                        "流式" if on_delta is not None else "非流式",
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    raise
        raise RuntimeError("LLM retry loop exited unexpectedly")

    # ── 内部：非流式调用 ─────────────────────────────

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
            response = await self._invoke_with_retry(
                runner_messages,
                self._agent.tool_registry.get_all_schema_openai(),
                max_tokens=self._agent.agent_config.max_tokens,
                run_ctx=run_ctx,
            )
            if response.usage:
                s.set_attr("tokens", response.usage)

        if response.usage:
            session.update_token_cost(
                response.usage["prompt"],
                response.usage["completion"],
                response.usage["total"],
            )

        messages.append(
            Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

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

        tool_msgs = await self._execute_tools(response.tool_calls, run_ctx)
        messages.extend(tool_msgs)

        if response.content:
            print(response.content)

        return True

    # ── 内部：流式调用 ───────────────────────────────

    async def _stream(
        self,
        session: Session,
        messages: list[Message],
        runner_messages: list[Message],
        run_ctx: RunContext,
    ) -> bool:
        """调用 LLM 并执行工具（流式）。

        流式增量经 on_stream_delta hook 事件——渲染层等订阅方
        工作在 hook 信息之上（llm 侧 on_delta 支持 awaitable，
        hook 是 async 也按序触发）。

        Args:
            session: 会话（usage 统计写入对象）。
            messages: 工作副本——追加 assistant 与 tool 消息。
            runner_messages: 实际发送给 LLM 的消息（governor 处理过）。
            run_ctx: 回合上下文。

        Returns:
            True 表示存在工具调用需继续循环；False 表示已出最终回答
            （空响应时经 hook 发送兜底提示）。
        """
        async with span("llm_call", model=self._agent.llm.model_id, stream=True) as s:
            response = await self._invoke_with_retry(
                runner_messages,
                self._agent.tool_registry.get_all_schema_openai(),
                max_tokens=self._agent.agent_config.max_tokens,
                run_ctx=run_ctx,
                on_delta=lambda delta: self._agent._hooks.on_stream_delta(run_ctx, delta),
            )
            if response.usage:
                s.set_attr("tokens", response.usage)
            if not (response.content and response.content.strip()) and not response.tool_calls:
                s.set_attr("empty_response", True)
            # finish=length = 生成被 max_tokens 截断（reasoning 模型思考段
            # 吃预算后正文到一半断掉）——span 记录现场供诊断
            if response.finish_reason == "length":
                s.set_attr("truncated", True)
                s.set_attr("reasoning_len", len(response.reasoning_content or ""))

        has_text = bool(response.content and response.content.strip())
        messages.append(
            Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

        if response.usage:
            session.update_token_cost(
                response.usage["prompt"],
                response.usage["completion"],
                response.usage["total"],
            )

        if response.tool_calls:
            tool_msgs = await self._execute_tools(response.tool_calls, run_ctx)
            messages.extend(tool_msgs)
            return True
        else:
            if not has_text:
                logger.warning(
                    "LLM 返回空响应（无文本、无工具调用）。usage=%s finish=%s",
                    getattr(response, "usage", {}),
                    getattr(response, "finish_reason", "?"),
                )
                # 给个兜底提示（经 hook 事件——渲染层统一显示）
                await self._agent._hooks.on_stream_delta(run_ctx, "_(模型未生成回答，请重试)_")
            elif response.finish_reason == "length":
                # 截断必须对用户可见——半截回答会被当作完整回答落盘
                logger.warning(
                    "回答被 max_tokens 截断（finish=length, usage=%s, reasoning=%d chars）",
                    getattr(response, "usage", {}),
                    len(response.reasoning_content or ""),
                )
                await self._agent._hooks.on_stream_delta(
                    run_ctx, "\n\n_(回答被 token 上限截断——调大 AGENT_MAX_TOKENS 或让我继续)_"
                )
            return False


# ════════════════════════════════════════════════════════════════
#  ReActAgent
# ════════════════════════════════════════════════════════════════


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
        wiki_dir: str | Path | None = None,
        hooks: list[AgentHook] | None = None,
        agent_config=None,
        compile_config: CompileConfig | None = None,
        retry_config: RetryConfig | None = None,
        issue_service: IssueService | None = None,
    ):
        super().__init__(name=name, workspace=workspace)
        self.llm = llm
        # vlm 供 /refine 等编译类命令使用（CompilePipeline 需要）
        self.vlm = vlm
        self.agent_config = agent_config or AgentCfg()
        # 由组装入口传入 RootConfig.compile；默认仅保留给单元测试和
        # 直接构造 Agent 的兼容路径。
        self.compile_config = compile_config or CompileConfig()
        self.retry_config = retry_config or RetryConfig()
        self.session_manager = SessionManager(workspace=workspace)
        self.tool_registry = tool_registry
        self.memory_store = MemoryStore(workspace=workspace)
        self.issue_service = issue_service or IssueService(IssueStore(workspace))
        self.tool_registry.register(RecordCorrection(self.issue_service))
        self.context_builder = ContextBuilder(
            system_prompt=self.SYSTEM_PROMPT,
            tool_registry=tool_registry,
            memory_store=self.memory_store,
            issue_service=self.issue_service,
            # wiki 目录显式传入（CLI 从配置解析）——build 时读
            # purpose/schema/index 组装环境块
            wiki_dir=wiki_dir,
            agent_config=self.agent_config,
        )
        self.context_governor = ContextGovernor(workspace=workspace, agent_config=self.agent_config)
        self.consolidator = Consolidator(
            consolidate_ratio=self.agent_config.consolidate_ratio,
            trigger_ratio=self.agent_config.trigger_ratio,
        )
        self.commands = create_command_router()
        self.dreamer = Dreamer(workspace=workspace, memory_store=self.memory_store)
        self._dream_task: asyncio.Task | None = None
        # 配置单一来源——直接读 agent_config（frozen 契约），
        # 不再维护 dict 视图（双真相：改配置忘同步视图就分叉）
        self.max_loop = self.agent_config.max_loop

        # ── hooks ─────────────────────────────────────────
        _raw = hooks or []
        self._hooks: AgentHook = (
            CompositeHook(_raw) if len(_raw) > 1 else _raw[0] if _raw else AgentHook()
        )

        # ── runner ────────────────────────────────────────
        self._runner = ReActRunner(self)

    async def _run(
        self,
        session_key: str,
        user_input: str,
        stream=False,
        run_id: str | None = None,
    ):
        self._ensure_dream_task()
        # 锁覆盖整个 turn，避免两个 turn 基于同一旧 history 生成回答
        # 后交错写回，导致 history/token cost/compaction/checkpoint 覆盖。
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
        """取消时恢复未提交的本轮状态；历史消息本来尚未追加。"""
        session.last_consolidated = snapshot["last_consolidated"]
        session.last_summary = snapshot["last_summary"]
        session.current_window_tokens = snapshot["current_window_tokens"]
        # token_cost 表示已经实际发生的 LLM 消耗；取消不应伪造为未发生。
        # 它不等于本轮是否成功写入 history。
        session.updated_at = snapshot["updated_at"]

    async def _notify_cancelled(self, run_ctx: RunContext, reason: str) -> None:
        """取消路径的最后收尾；清理失败不能掩盖原始取消。"""
        run_ctx.stop_reason = "cancelled"
        run_ctx.error = reason
        run_ctx.exception = asyncio.CancelledError(reason)
        try:
            await asyncio.shield(self._runner.cancel_active_tools())
            await asyncio.shield(self._hooks.on_run_error(run_ctx))
            # renderer 的 on_run_end 负责关闭未完成的 stream/tool UI；
            # 这里虽非成功结束，但必须执行其清理语义。
            await asyncio.shield(self._hooks.on_run_end(run_ctx))
        except Exception as exc:
            logger.warning("取消收尾失败: %s: %s", type(exc).__name__, str(exc)[:160])

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
        # closed 只表示上一轮 idle 收尾完成；用户重新输入时恢复活动态。
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
                await self._hooks.on_run_error(run_ctx)
                await self._hooks.on_run_end(run_ctx)
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
        """执行一轮完整对话（restore → 命令分发 → 压缩 → 回答 → save）。

        Args:
            session_key: 会话标识。
            user_input: 用户输入文本。
            stream: 为 True 时使用流式调用。
        """
        await self._hooks.on_run_start(run_ctx)

        # command — 命令在 restore 之后、压缩之前分发
        # 命令需要 session 状态，但不应触发昂贵的 LLM 压缩
        cmd_result = await self.commands.dispatch(
            user_input.strip(), session, self, run_context=run_ctx
        )
        if cmd_result is not None:
            if cmd_result.text:
                # 命令输出经流式增量事件——渲染层订阅 hook 统一显示
                await self._hooks.on_stream_delta(run_ctx, cmd_result.text + "\n\n")
            if cmd_result.rerun_with:
                # /retry 类命令：替换 user_input 继续走完整流程
                # （历史原封不动，追加的"不满意"指令就是新 user 消息）
                user_input = cmd_result.rerun_with
            else:
                # 命令路径不跑 LLM loop——手动收尾 run 事件
                # （正常路径在 run_loop 结束后 on_run_end）
                await self._hooks.on_run_end(run_ctx)
                return

        # compact
        # 对会话进行压缩
        await self._hooks.on_status(run_ctx, "compacting")
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
            # 这里不需要锁，因为session之间在while下一定是串行的，后面改成消息队列的话再处理
            saved = await asyncio.to_thread(self.session_manager.save_checkpoint, session=session)
            if saved:
                # 压缩是历史状态整理；一旦 checkpoint 成功，即使本轮
                # 回答后来取消，也保留它，避免下一轮重复压缩。
                turn_state["compaction_persisted"] = True
            # 单用户模式：所有session共享同一个history
            await asyncio.to_thread(
                self.memory_store.append_history, session=session, summary=session.last_summary
            )
            session.last_memory_archived = len(session.history)

        current_message = Message(role="user", content=user_input)

        # build
        # 注意这里的history是未压缩的部分，长度比真实的historyfile小
        history = session.get_history(max_messages_length=self.agent_config.max_messages_length)
        messages = self.context_builder.build_messages(
            session=session,
            current_message=current_message,
            history=history,
            last_summary=session.last_summary,
        )
        initail_message_count = len(messages)

        # RUN
        await self._runner.run_loop(session, messages, stream, run_ctx)

        # SAVE
        # 这里应该补充会话数据清洗，清洗掉错误的工具调用，空的assisstant回复，超大的工具结果，将内容保存成文件
        # 消息内容替换成文件的引用
        get_skip_count = self._get_skip_count(
            initial_message_count=initail_message_count,
        )

        await asyncio.to_thread(session.add_messages, messages[get_skip_count:])
        await asyncio.to_thread(self.session_manager.save_checkpoint, session=session)

        # on_run_end
        run_ctx.final_content = messages[-1].content if messages else ""
        await self._hooks.on_run_end(run_ctx)

    def _ensure_dream_task(self) -> None:
        """首次运行时启动 idle 收尾与 Dream 后台任务。"""
        if self._dream_task is None or self._dream_task.done():
            self._dream_task = asyncio.create_task(
                self._dream_loop(self.agent_config.dream_poll_interval),
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
                # 等锁期间可能已有新消息，重新检查而不是盲目收尾。
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
                await asyncio.to_thread(self.session_manager.save_checkpoint, session=session)
        return changed

    async def _dream_loop(self, interval: int = 60):
        """定期结束 idle session，并处理已落账的 Dream 输入。

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
                # Dream 是后台维护任务，失败不能杀死 Agent 主循环；下个
                # poll 会再次尝试，且 Dreamer 不会在失败时推进游标。
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
        # 在nanobot上，情况稍微复杂点，因为作者想要崩溃时保存住用户的消息，为了防止突然崩溃，会提前将合并的部分写入磁盘
        # 所以合并不合并的起始在这种情况下就改变了。但是我的设计逻辑是，任何情况下history都存储的是上一轮结束的结果
        # 如果这一轮崩溃，自然回退到上一轮，代价是用户需要重新输入一遍。
        return initial_message_count - 1
