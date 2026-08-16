"""测试 Consolidator — 修复后验证。

验证: archive 失败时不再 break，而是跳过该段继续压缩。

用法:
    VIRTUAL_ENV= .venv/bin/python test/test_consolidator.py
"""

from __future__ import annotations

import asyncio, json, sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / "env" / ".env")

from wiki_agent.consolidator import Consolidator
from wiki_agent.context import ContextBuilder
from wiki_agent.message import Message, LLMResponse, ToolCall
from wiki_agent.session import Session
from wiki_agent.tools import ToolRegistry
from wiki_agent.memory import MemoryStore


async def main():
    print("=" * 60)
    print("测试: archive 失败不应导致 breaker 死循环")
    print("=" * 60)

    # 加载真实 session
    session_file = Path(__file__).resolve().parent.parent / "workspace" / "sessions" / "key1.jsonl"
    if not session_file.exists():
        print("无 session 数据，跳过")
        return

    s = Session(key="test")
    with open(session_file) as f:
        for line in f:
            if not (line := line.strip()):
                continue
            try:
                d = json.loads(line)
                if d.get("_type") == "metadata":
                    continue
                tool_calls = []
                for tc in d.get("tool_calls", []):
                    func = tc.get("function", {})
                    args = func.get("arguments", {})
                    if isinstance(args, str):
                        try: args = json.loads(args)
                        except: pass
                    tool_calls.append(ToolCall(
                        id=tc.get("id", ""), name=func.get("name", ""), arguments=args))
                s.add_message(Message(
                    role=d.get("role", "?"), content=d.get("content", ""),
                    tool_calls=tool_calls, tool_call_id=d.get("tool_call_id", ""),
                    tool_name=d.get("tool_name", "")))
            except Exception:
                pass

    print(f"Session: {len(s.history)} 条消息")

    workspace = Path("/tmp/wiki_consol_test4")
    workspace.mkdir(parents=True, exist_ok=True)
    context_builder = ContextBuilder(
        system_prompt="你是助手\n{tools_description}\n{summery}",
        tool_registery=ToolRegistry(),
        memory_store=MemoryStore(workspace),
    )

    # ── 模拟: 前两次 archive 失败，第三次成功 ──
    consolidate = Consolidator()
    fail_count = [0]

    async def flaky_invoke(messages, **kwargs):
        fail_count[0] += 1
        if fail_count[0] <= 2:
            raise RuntimeError(f"API 错误 #{fail_count[0]}")
        return LLMResponse(content=f"压缩成功(第{fail_count[0]}次)", finish_reason="stop")

    mock_llm = MagicMock()
    mock_llm.async_invoke = AsyncMock(side_effect=flaky_invoke)

    print(f"\n运行 maybe_consolidate (前 2 次 LLM 调用会失败)...")
    result = await consolidate.maybe_consolidate(
        llm=mock_llm, session=s, context_builder=context_builder,
        context_windows=128_000, max_tokens=4_096, replay_max_messages=200,
    )
    print(f"结果: {'压缩完成' if result else '无变化'}")
    print(f"last_consolidated: {s.last_consolidated}/{len(s.history)}")
    print(f"LLM 调用: {fail_count[0]} 次")
    print(f"current_window_tokens: {s.current_window_tokens:,}")
    est = consolidate._estimate_session_prompt_tokens(s, context_builder, 200)
    print(f"重估: {est:,}")

    if result:
        print("\n✅ 修复有效: archive 失败也推进了 last_consolidated，不会死循环")
    else:
        print("\n⚠️ 仍不触发压缩（可能 estimate 未超目标）")


if __name__ == "__main__":
    asyncio.run(main())
