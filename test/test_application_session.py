from pathlib import Path

from wiki_agent.application.session import SessionService
from wiki_agent.conversation import Message, Session, SessionManager, ThinkingSegment, ToolCall


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

