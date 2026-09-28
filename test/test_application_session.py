import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from wiki_agent.application.session import SessionService
from wiki_agent.conversation import Message, Session, SessionManager, ThinkingSegment, ToolCall
from wiki_agent.events import AgentEvent


def test_title_from_query_normalizes_and_truncates() -> None:
    assert SessionService.title_from_query("  如何  编译 Wiki？ ") == "如何 编译 Wiki？"
    assert SessionService.title_from_query("abcdefgh", max_length=5) == "abcd…"
    assert SessionService.title_from_query("   ") == "未命名"


def test_session_info_counts_only_visible_chat_messages() -> None:
    session = Session(key="session_test")
    session.history = [
        Message(role="user", content="问题"),
        Message(role="assistant", content="回答"),
        Message(role="assistant", content=""),
        Message(role="tool", content="内部工具结果"),
    ]

    info = SessionService._to_info(session)

    assert info.message_count == 2


def test_get_session_messages_aggregates_thinking_segments(tmp_path: Path) -> None:
    """工具回合与最终回合的 thinking 分段按顺序聚合到可见回答上。"""
    manager = SessionManager(tmp_path)
    session = Session(key="session_think")
    session.history = [
        Message(role="user", content="问题"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="t1", name="Grep", arguments={"pattern": "x"})],
            thinking=[
                ThinkingSegment(kind="think", text="先查一下"),
                ThinkingSegment(kind="tool", name="Grep", arguments={"pattern": "x"}, ms=420),
            ],
        ),
        Message(role="tool", tool_call_id="t1", tool_name="Grep", content="结果"),
        Message(
            role="assistant",
            content="回答",
            thinking=[ThinkingSegment(kind="think", text="确认答案")],
        ),
    ]
    assert manager.save_checkpoint(session)

    service = SessionService(agent=None, session_manager=manager, event_publisher=None)
    messages = service.get_session_messages("session_think")

    assert [message.role for message in messages] == ["user", "assistant"]
    segments = messages[1].thinking
    assert [seg["kind"] for seg in segments] == ["think", "tool", "think"]
    assert segments[1]["name"] == "Grep"
    assert segments[1]["ms"] == 420
    assert "text" not in segments[1]  # exclude_defaults：tool 段不带空文字字段


class _FakePublisher:
    """只提供一回合终结事件的假发布器。"""

    def __init__(self, stop_reason=None):
        self.stop_reason = stop_reason

    @asynccontextmanager
    async def subscribe(self, run_id):
        queue = asyncio.Queue()
        await queue.put(
            AgentEvent(
                run_id=run_id,
                session_id="session_title",
                sequence=0,
                type="run_finished",
                data={"stop_reason": self.stop_reason},
            )
        )
        yield queue


class _FakeAgent:
    def __init__(self, manager, title="Wiki 编译流程", fail=False):
        self._manager = manager
        self._title = title
        self._fail = fail
        self.title_calls = 0

    async def run(self, *, session_key, user_input, stream, run_id):
        session = self._manager.get_or_create(session_key)
        session.add_message(Message(role="user", content=user_input))
        session.add_message(Message(role="assistant", content="回答正文"))

    async def generate_session_title(self, question, answer):
        self.title_calls += 1
        if self._fail:
            raise RuntimeError("network boom")
        return self._title


def _title_service(tmp_path, *, fail=False, stop_reason=None):
    manager = SessionManager(tmp_path)
    assert manager.save_checkpoint(Session(key="session_title"))
    agent = _FakeAgent(manager, fail=fail)
    service = SessionService(
        agent=agent,
        session_manager=manager,
        event_publisher=_FakePublisher(stop_reason),
    )
    return service, agent, manager


def test_first_turn_writes_llm_title_once(tmp_path: Path) -> None:
    """标题只落一次盘：回合结束直接是定名，无占位中间态。"""
    service, agent, manager = _title_service(tmp_path)

    async def _ask():
        result = await service.send_message("session_title", "怎么重新编译 wiki？")
        # 回合返回时定名已写完（send_message 同步等待标题任务）
        assert result.session.title == "Wiki 编译流程"

    asyncio.run(_ask())
    assert agent.title_calls == 1
    session = manager.get_or_create("session_title")
    assert session.session_title == "Wiki 编译流程"


def test_title_failure_falls_back_to_truncation(tmp_path: Path) -> None:
    service, _, manager = _title_service(tmp_path, fail=True)

    async def _ask():
        await service.send_message("session_title", "怎么重新编译 wiki？")

    asyncio.run(_ask())

    session = manager.get_or_create("session_title")
    assert session.session_title == "怎么重新编译 wiki？"


def test_cancelled_turn_skips_title_generation(tmp_path: Path) -> None:
    service, agent, manager = _title_service(tmp_path, stop_reason="cancelled")

    async def _ask():
        await service.send_message("session_title", "怎么重新编译 wiki？")

    asyncio.run(_ask())

    assert agent.title_calls == 0
    assert manager.get_or_create("session_title").session_title == "未命名"


def test_later_turns_do_not_regenerate_title(tmp_path: Path) -> None:
    service, agent, _ = _title_service(tmp_path)

    async def _two_turns():
        await service.send_message("session_title", "第一轮问题")
        await service.send_message("session_title", "第二轮追问")

    asyncio.run(_two_turns())

    assert agent.title_calls == 1


def test_manual_rename_not_overwritten(tmp_path: Path) -> None:
    """定名生成期间用户已手动改名——结果放弃，尊重用户输入。"""
    service, agent, manager = _title_service(tmp_path)
    original_generate = agent.generate_session_title

    async def _rename_during_generation(question, answer):
        session = manager.get_or_create("session_title")
        session.session_title = "我自己起的名字"
        return await original_generate(question, answer)

    agent.generate_session_title = _rename_during_generation

    async def _ask():
        await service.send_message("session_title", "怎么重新编译 wiki？")

    asyncio.run(_ask())

    assert manager.get_or_create("session_title").session_title == "我自己起的名字"
