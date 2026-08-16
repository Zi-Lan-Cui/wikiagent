"""多用户并发测试 — 不需要真实 LLM，全部 mock 瞬间完成"""

import asyncio
import json
import time
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from wiki_agent.message import LLMResponse
from wiki_agent.llm import LLMClient
from wiki_agent.tools import ToolRegistry
from wiki_agent.agent import ReActAgent


# ─── 辅助 ───

def _make_agent(tmp_path: Path) -> ReActAgent:
    llm = LLMClient()
    llm.api_key = "mock"
    llm.base_url = "mock"
    llm.model_id = "mock"
    llm._initailized = True
    tool_registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return ReActAgent(name="test", llm=llm, vlm=llm, tool_registry=tool_registry, workspace=workspace)


def _fake_response(content="你好！", usage=None):
    if usage is None:
        usage = {"prompt": 100, "completion": 50, "total": 150}
    return LLMResponse(content=content, tool_calls=[], finish_reason="stop", usage=usage)


async def _mock_consolidate(llm, session, **kw):
    """模拟压缩发生：设置 last_summery 并返回 True → append_history 会执行"""
    session.last_summery = "压缩摘要"
    return True


def _read_history(agent, user_id):
    hf = agent.memory_store.get_history_file(user_id)
    if not hf.exists():
        return []
    return [json.loads(l) for l in hf.read_text().strip().splitlines() if l.strip()]


# ═══════════════════════════════════════════
#  单用户多 session 并发
# ═══════════════════════════════════════════

@pytest.mark.asyncio
class TestSameUserMultiSession:

    async def test_two_sessions_concurrent_no_data_loss(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(
            side_effect=lambda messages, *a, **kw:
                _fake_response(content=f"回复 {messages[-1].content}")
        )
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        await asyncio.gather(
            agent._run(session_key="key_a", user_id="u1", user_input="用户A", stream=False),
            agent._run(session_key="key_b", user_id="u1", user_input="用户B", stream=False),
        )

        recs = _read_history(agent, "u1")
        assert len(recs) == 2
        cursors = sorted([r["cursor"] for r in recs])
        assert cursors == [1, 2], f"cursor={cursors}"

        s_a = agent.session_manager.get_or_create("key_a", "u1")
        s_b = agent.session_manager.get_or_create("key_b", "u1")
        assert len(s_a.history) >= 2
        assert len(s_b.history) >= 2
        assert s_a.token_cost["total"] > 0
        assert s_b.token_cost["total"] > 0

    async def test_many_sessions_stress(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(return_value=_fake_response(content="ok"))
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        N = 10
        tasks = [
            agent._run(session_key=f"key_{i:02d}", user_id="u1",
                       user_input=f"msg{i:02d}", stream=False)
            for i in range(N)
        ]

        start = time.perf_counter()
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        recs = _read_history(agent, "u1")
        assert len(recs) == N, f"应有 {N} 条，实际 {len(recs)}"
        cursors = sorted([r["cursor"] for r in recs])
        assert cursors == list(range(1, N + 1)), f"cursor={cursors}"

        for i in range(N):
            s = agent.session_manager.get_or_create(f"key_{i:02d}", "u1")
            assert len(s.history) >= 2

        print(f"\n  10 session → {elapsed:.2f}s")

    async def test_two_turns_each(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(return_value=_fake_response(content="回复"))
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        await asyncio.gather(
            agent._run(session_key="key_a", user_id="u1", user_input="第1轮A", stream=False),
            agent._run(session_key="key_b", user_id="u1", user_input="第1轮B", stream=False),
        )
        await asyncio.gather(
            agent._run(session_key="key_a", user_id="u1", user_input="第2轮A", stream=False),
            agent._run(session_key="key_b", user_id="u1", user_input="第2轮B", stream=False),
        )

        recs = _read_history(agent, "u1")
        assert len(recs) == 4
        cursors = sorted([r["cursor"] for r in recs])
        assert cursors == [1, 2, 3, 4]

    async def test_session_data_not_crossed(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        call_count = [0]

        async def mock_invoke(messages, *a, **kw):
            call_count[0] += 1
            last = messages[-1].content
            return _fake_response(content=f"回复{call_count[0]}: {last}")

        agent.llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        N = 5
        await asyncio.gather(*[
            agent._run(session_key=f"key_{i:02d}", user_id="u1",
                       user_input=f"msg{i:02d}", stream=False)
            for i in range(N)
        ])

        for i in range(N):
            s = agent.session_manager.get_or_create(f"key_{i:02d}", "u1")
            user_msgs = [m.content for m in s.history if m.role == "user"]
            assert len(user_msgs) == 1
            assert user_msgs[0] == f"msg{i:02d}", f"session {i} user_msg={user_msgs[0]}"


# ═══════════════════════════════════════════
#  多用户隔离
# ═══════════════════════════════════════════

@pytest.mark.asyncio
class TestMultiUserConcurrency:

    async def test_different_users_isolated(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(return_value=_fake_response(content="回复"))
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        await asyncio.gather(
            agent._run(session_key="s_a", user_id="user_alice", user_input="Alice", stream=False),
            agent._run(session_key="s_b", user_id="user_bob", user_input="Bob", stream=False),
        )

        recs_a = _read_history(agent, "user_alice")
        recs_b = _read_history(agent, "user_bob")
        assert len(recs_a) == 1
        assert len(recs_b) == 1
        assert recs_a[0]["cursor"] == 1
        assert recs_b[0]["cursor"] == 1

    async def test_same_user_cursor_no_gaps(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(return_value=_fake_response(content="ok"))
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        N = 20
        await asyncio.gather(*[
            agent._run(session_key=f"key_{i:02d}", user_id="u1",
                       user_input=f"msg{i}", stream=False)
            for i in range(N)
        ])

        recs = _read_history(agent, "u1")
        assert len(recs) == N
        cursors = sorted([r["cursor"] for r in recs])
        assert cursors == list(range(1, N + 1)), f"cursor={cursors}"


# ═══════════════════════════════════════════
#  Token 计费
# ═══════════════════════════════════════════

@pytest.mark.asyncio
class TestTokenCostIsolation:

    async def test_each_session_tracks_own_cost(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(return_value=
            _fake_response(usage={"prompt": 10, "completion": 5, "total": 15})
        )
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        await asyncio.gather(
            agent._run(session_key="key_a", user_id="u1", user_input="A", stream=False),
            agent._run(session_key="key_b", user_id="u1", user_input="B", stream=False),
        )

        s_a = agent.session_manager.get_or_create("key_a", "u1")
        s_b = agent.session_manager.get_or_create("key_b", "u1")
        assert s_a.token_cost["total"] == 15
        assert s_b.token_cost["total"] == 15

    async def test_token_persisted_and_loaded(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(return_value=
            _fake_response(usage={"prompt": 20, "completion": 10, "total": 30})
        )
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        await agent._run(session_key="key_a", user_id="u1", user_input="hi", stream=False)

        agent2 = _make_agent(tmp_path)
        s = agent2.session_manager.get_or_create("key_a", "u1")
        assert s.token_cost["total"] == 30
        assert s.token_cost["prompt"] == 20


# ═══════════════════════════════════════════
#  异常隔离
# ═══════════════════════════════════════════

@pytest.mark.asyncio
class TestFailureIsolation:

    async def test_one_fails_other_succeeds(self, tmp_path: Path):
        agent = _make_agent(tmp_path)

        async def mock_invoke(messages, *a, **kw):
            last = messages[-1].content
            if "炸" in last:
                raise RuntimeError("LLM 挂了")
            return _fake_response(content="正常回复")

        agent.llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        results = await asyncio.gather(
            agent._run(session_key="key_a", user_id="u1", user_input="让我炸", stream=False),
            agent._run(session_key="key_b", user_id="u1", user_input="正常请求", stream=False),
            return_exceptions=True,
        )
        assert isinstance(results[0], Exception)
        assert results[1] is None

        s_b = agent.session_manager.get_or_create("key_b", "u1")
        assert len(s_b.history) >= 2

    async def test_same_session_serial_naturally(self, tmp_path: Path):
        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(return_value=_fake_response(content="回复"))
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        await agent._run(session_key="same_key", user_id="u1", user_input="A", stream=False)
        await agent._run(session_key="same_key", user_id="u1", user_input="B", stream=False)

        s = agent.session_manager.get_or_create("same_key", "u1")
        user_msgs = [m.content for m in s.history if m.role == "user"]
        assert len(user_msgs) == 2


# ═══════════════════════════════════════════
#  Tool calls 并发
# ═══════════════════════════════════════════

@pytest.mark.asyncio
class TestConcurrencyWithTools:

    async def test_concurrent_sessions_with_tool_calls(self, tmp_path: Path):
        from wiki_agent.message import ToolCall

        call_count = [0]

        async def mock_invoke(messages, *a, **kw):
            call_count[0] += 1
            if call_count[0] % 2 == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id=f"c_{call_count[0]}", name="echo", arguments={})],
                    finish_reason="tool_calls",
                    usage={"prompt": 10, "completion": 0, "total": 10},
                )
            else:
                return _fake_response(content="完成")

        agent = _make_agent(tmp_path)
        agent.llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.consolidator.maybe_consolidate = AsyncMock(side_effect=_mock_consolidate)

        class EchoTool:
            name = "echo"
            description = "echo"
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kw):
                return "echo ok"

        agent.tool_registery.register(EchoTool())

        await asyncio.gather(
            agent._run(session_key="key_a", user_id="u1", user_input="A", stream=False),
            agent._run(session_key="key_b", user_id="u1", user_input="B", stream=False),
        )
        assert call_count[0] == 4
