"""Consolidator 测试——双策略/游标推进/防死循环。

无 session 数据依赖（旧版读 workspace/key1.jsonl 的脆弱方式废弃）：
全部用脚本化 LLM + 构造 session。

直接运行:  .venv/bin/python test/test_consolidator.py
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from wiki_agent.consolidator import Consolidator
from wiki_agent.context import ContextBuilder
from wiki_agent.message import LLMResponse, Message
from wiki_agent.memory import MemoryStore
from wiki_agent.session import Session
from wiki_agent.tools import ToolRegistry


def _make_consolidator(tmp: Path, session: Session) -> tuple[Consolidator, ContextBuilder]:
    builder = ContextBuilder(
        system_prompt="你是助手\n{tools_description}\n{summery}",
        tool_registery=ToolRegistry(),
        memory_store=MemoryStore(workspace=tmp),
    )
    return Consolidator(), builder


def _long_session(n_rounds: int = 30) -> Session:
    """构造长对话——每轮 user+assistant，内容足够长触发压缩。"""
    s = Session(key="test")
    for i in range(n_rounds):
        s.add_messages([
            Message(role="user", content=f"第{i}个问题 " + "内容" * 200),
            Message(role="assistant", content=f"第{i}个回答 " + "内容" * 200),
        ])
    return s


def test_archive_failure_advances_cursor_no_dead_loop():
    """archive 失败仍推进游标——不死循环（原修复的回归锁定）。"""
    async def run():
        tmp = Path(tempfile.mkdtemp())
        s = _long_session(40)
        consolidator, builder = _make_consolidator(tmp, s)

        calls = [0]

        async def flaky_invoke(messages, **kwargs):
            calls[0] += 1
            if calls[0] <= 2:
                raise RuntimeError(f"API 错误 #{calls[0]}")
            return LLMResponse(content="压缩成功", finish_reason="stop")

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=flaky_invoke)

        result = await consolidator.maybe_consolidate(
            llm=mock_llm, session=s, context_builder=builder,
            context_windows=128_000, max_tokens=4_096,
            replay_max_messages=10,  # 窗口小——容易触发策略 1
        )
        # 失败也推进了游标（不死循环的核心断言）
        assert s.last_consolidated > 0
        # 有调用发生
        assert calls[0] > 0
        # 返回 True（发生了压缩推进）
        assert result is True
    asyncio.run(run())


def test_empty_session_no_consolidation():
    async def run():
        tmp = Path(tempfile.mkdtemp())
        s = Session(key="empty")
        consolidator, builder = _make_consolidator(tmp, s)

        mock_llm = MagicMock()
        result = await consolidator.maybe_consolidate(
            llm=mock_llm, session=s, context_builder=builder,
            context_windows=128_000, max_tokens=4_096, replay_max_messages=10,
        )
        assert result is False
        assert s.last_consolidated == 0
        assert mock_llm.async_invoke.call_count == 0
    asyncio.run(run())


def test_strategy1_replay_overflow_compresses_invisible():
    """策略 1: 窗口外消息（LLM 已不可见）被压缩，游标推进。"""
    async def run():
        tmp = Path(tempfile.mkdtemp())
        s = _long_session(30)
        consolidator, builder = _make_consolidator(tmp, s)

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=LLMResponse(
            content="旧对话摘要", finish_reason="stop"))

        result = await consolidator.maybe_consolidate(
            llm=mock_llm, session=s, context_builder=builder,
            context_windows=128_000, max_tokens=4_096,
            replay_max_messages=5,  # 只保留最近 5 条——前面 55 条是窗口外
        )
        assert s.last_consolidated > 0
        # 摘要更新了
        assert s.last_summery == "旧对话摘要"
        assert mock_llm.async_invoke.call_count >= 1
    asyncio.run(run())


def test_strategy2_water_level_triggers():
    """策略 2: 窗口内超 trigger 水位触发压缩。"""
    async def run():
        tmp = Path(tempfile.mkdtemp())
        s = _long_session(50)
        consolidator, builder = _make_consolidator(tmp, s)

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=LLMResponse(
            content="压缩摘要", finish_reason="stop"))

        await consolidator.maybe_consolidate(
            llm=mock_llm, session=s, context_builder=builder,
            context_windows=8_000, max_tokens=1_000,  # 小窗口——窗口内超水位
            replay_max_messages=5,
        )
        assert s.last_consolidated > 0
        assert mock_llm.async_invoke.call_count >= 1
        # 窗口 token 已更新
        assert s.current_window_tokens > 0
    asyncio.run(run())


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
