from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from pathlib import Path

from wiki_agent.agent.base import BaseAgent
from wiki_agent.command import create_command_router
from wiki_agent.config import AgentConfig as AgentCfg
from wiki_agent.consolidator import Consolidator
from wiki_agent.context import ContextBuilder, ContextGovernor
from wiki_agent.hook import AgentHook, CompositeHook, RunContext
from wiki_agent.llm import LLMClient
from wiki_agent.log import begin_trace, get_logger, span
from wiki_agent.memory import Dreamer, MemoryStore
from wiki_agent.message import Message
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

    async def run_loop(
        self, session: Session, messages: list[Message], stream: bool,
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
        self, tool_calls: list, run_ctx: RunContext,
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
                    result = await self._agent.tool_registery.execute(tc.name, params=tc.arguments)
                    await self._agent._hooks.on_tool_result(
                        context=run_ctx, tool_name=tc.name,
                        tool_call_id=tc.id, result=result,
                    )
                    run_ctx.tools_used.append(tc.name)
                    s.set_attr("result_len", len(str(result)))
                    return tc, result, None
                except Exception as exc:
                    await self._agent._hooks.on_tool_error(
                        context=run_ctx, tool_name=tc.name,
                        tool_call_id=tc.id, error=exc,
                    )
                    return tc, f"工具执行错误: {exc}", exc

        results = await asyncio.gather(*[_run_one(tc) for tc in tool_calls])

        tool_msgs: list[Message] = []
        for tc, result, _exc in results:
            tool_msgs.append(Message(
                role="tool",
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=str(result),
            ))
        return tool_msgs

    # ── 内部：非流式调用 ─────────────────────────────

    async def _invoke(
        self, session: Session, messages: list[Message],
        runner_messages: list[Message], run_ctx: RunContext,
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
            response = await self._agent.llm.async_invoke(
                runner_messages,
                tools=self._agent.tool_registery.get_all_schema_openai(),
                max_tokens=self._agent.agent_config.max_tokens,
            )
            if response.usage:
                s.set_attr("tokens", response.usage)

        if response.usage:
            session.update_token_cost(
                response.usage["prompt"],
                response.usage["completion"],
                response.usage["total"],
            )

        messages.append(Message(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
        ))

        if not response.tool_calls:
            if response.content:
                print(response.content)
            return False

        tool_msgs = await self._execute_tools(response.tool_calls, run_ctx)
        messages.extend(tool_msgs)

        if response.content:
            print(response.content)

        return True

    # ── 内部：流式调用 ───────────────────────────────

    async def _stream(
        self, session: Session, messages: list[Message],
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
            response = await self._agent.llm.async_stream(
                runner_messages,
                tools=self._agent.tool_registery.get_all_schema_openai(),
                max_tokens=self._agent.agent_config.max_tokens,
                on_delta=lambda delta: self._agent._hooks.on_stream_delta(
                    run_ctx, delta),
            )
            if response.usage:
                s.set_attr("tokens", response.usage)
            if not (response.content and response.content.strip()) and not response.tool_calls:
                s.set_attr("empty_response", True)

        has_text = bool(response.content and response.content.strip())
        has_calls = bool(response.tool_calls)

        messages.append(Message(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
        ))

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
                    getattr(response, 'usage', {}),
                    getattr(response, 'finish_reason', '?'),
                )
                # 给个兜底提示（经 hook 事件——渲染层统一显示）
                await self._agent._hooks.on_stream_delta(
                    run_ctx, "_(模型未生成回答，请重试)_")
            return False


# ════════════════════════════════════════════════════════════════
#  ReActAgent
# ════════════════════════════════════════════════════════════════

class ReActAgent(BaseAgent):

    SYSTEM_PROMPT="""
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
        {summery}

        # 待处理纠错（回答时避开或提示这些页面存在问题）
        {corrections}
    """

    def __init__(
            self,
            name:str,
            llm:LLMClient,
            vlm:LLMClient,
            tool_registry:ToolRegistry,
            workspace:Path,
            wiki_dir:str|Path|None=None,
            hooks:list[AgentHook]|None=None,
            agent_config=None,
        ):
        super().__init__(name=name,workspace=workspace)
        self.llm=llm
        # vlm 供 /refine 等编译类命令使用（CompilePipeline 需要）
        self.vlm=vlm
        self.agent_config = agent_config or AgentCfg()
        self.session_manager=SessionManager(workspace=workspace)
        self.tool_registery=tool_registry
        self.memory_store=MemoryStore(workspace=workspace)
        # 纠错记录工具——agent 主动调用把 wiki 纠错落进待修清单。
        # agent 自己接线（memory_store 是 agent 的构造产物，
        # CLI 的 tool_registry 在 agent 之前构造，绑不上）
        self.tool_registery.register(RecordCorrection(self.memory_store))
        self.context_builder=ContextBuilder(
            system_prompt=self.SYSTEM_PROMPT,
            tool_registery=tool_registry,
            memory_store=self.memory_store,
            # wiki 目录显式传入（CLI 从配置解析）——build 时读
            # purpose/schema/index 组装环境块
            wiki_dir=wiki_dir,
            agent_config=self.agent_config,
        )
        self.context_governor=ContextGovernor(
            workspace=workspace, agent_config=self.agent_config)
        self.consolidator=Consolidator(
            consolidate_ratio=self.agent_config.consolidate_ratio,
            trigger_ratio=self.agent_config.trigger_ratio,
        )
        self.commands=create_command_router()
        self.dreamer=Dreamer(workspace=workspace,memory_store=self.memory_store)
        # 配置单一来源——直接读 agent_config（frozen 契约），
        # 不再维护 dict 视图（双真相：改配置忘同步视图就分叉）
        self.max_loop=self.agent_config.max_loop

        # ── hooks ─────────────────────────────────────────
        _raw = hooks or []
        self._hooks: AgentHook = (
            CompositeHook(_raw) if len(_raw) > 1
            else _raw[0] if _raw
            else AgentHook()
        )

        # ── runner ────────────────────────────────────────
        self._runner = ReActRunner(self)

    async def _run(
            self,
            session_key:str,
            user_input:str,
            stream=False,
        ):
        # 一次 run 一条 trace——贯穿 LLM 调用/工具执行/压缩全链路
        begin_trace()

        # turn 级观测——与编译链路的 ingest_file span 对齐：
        # 事件流能回答"这一次 run 整体耗时/成败"（span 只观察不吞异常）
        async with span("turn", session=session_key):
            await self._run_turn(
                session_key=session_key, user_input=user_input,
                stream=stream,
            )

    async def _run_turn(
            self,
            *,
            session_key: str,
            user_input: str,
            stream: bool,
        ):
        """执行一轮完整对话（restore → 命令分发 → 压缩 → 回答 → save）。

        Args:
            session_key: 会话标识。
            user_input: 用户输入文本。
            stream: 为 True 时使用流式调用。
        """
        run_ctx = RunContext(session_key=session_key)

        # restore
        session:Session=self.session_manager.get_or_create(session_key=session_key)
        await self._hooks.on_run_start(run_ctx)

        # command — 命令在 restore 之后、压缩之前分发
        # 命令需要 session 状态，但不应触发昂贵的 LLM 压缩
        cmd_result = await self.commands.dispatch(
            user_input.strip(), session, self)
        if cmd_result is not None:
            if cmd_result.text:
                # 命令输出经流式增量事件——渲染层订阅 hook 统一显示
                await self._hooks.on_stream_delta(
                    run_ctx, cmd_result.text + "\n\n")
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
            consolidated=await self.consolidator.maybe_consolidate(
                llm=self.llm,
                session=session,
                context_builder=self.context_builder,
                context_windows=self.agent_config.context_windows,
                max_tokens=self.agent_config.max_tokens,
                replay_max_messages=self.agent_config.max_messages_length,
            )
            s.set_attr("consolidated", consolidated)
            s.set_attr("last_consolidated", session.last_consolidated)

        if consolidated:
            # 这里不需要锁，因为session之间在while下一定是串行的，后面改成消息队列的话再处理
            await asyncio.to_thread(self.session_manager.save_checkpoint,session=session)
            # 单用户模式：所有session共享同一个history
            await asyncio.to_thread(self.memory_store.append_history,session=session,summery=session.last_summery)

        current_message=Message(role="user",content=user_input)

        # build
        # 注意这里的history是未压缩的部分，长度比真实的historyfile小
        history=session.get_history(max_messages_length=self.agent_config.max_messages_length)
        messages=self.context_builder.build_messages(
            session=session,
            current_message=current_message,
            history=history,
            last_summery=session.last_summery
        )
        initail_message_count=len(messages)

        # RUN
        await self._runner.run_loop(session, messages, stream, run_ctx)

        # SAVE
        # 这里应该补充会话数据清洗，清洗掉错误的工具调用，空的assisstant回复，超大的工具结果，将内容保存成文件
        # 消息内容替换成文件的引用
        get_skip_count=self._get_skip_count(
            initial_message_count=initail_message_count,
        )

        await asyncio.to_thread(session.add_messages,messages[get_skip_count:])
        await asyncio.to_thread(self.session_manager.save_checkpoint,session=session)

        # on_run_end
        run_ctx.final_content = messages[-1].content if messages else ""
        await self._hooks.on_run_end(run_ctx)

    async def _dream_loop(self,interval:int=3000):
        """定期触发记忆整理。

        Args:
            interval: 触发间隔（秒），默认 3000。单用户模式直接 dream。
        """
        while True:
            await asyncio.sleep(interval)
            await self.dreamer.dream(llm=self.llm)

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
        return initial_message_count-1
            