from wiki_agent.application.service import WikiAgentService
from wiki_agent.message import Message
from wiki_agent.session import Session


def test_title_from_query_normalizes_and_truncates() -> None:
    assert WikiAgentService.title_from_query("  如何  编译 Wiki？ ") == "如何 编译 Wiki？"
    assert WikiAgentService.title_from_query("abcdefgh", max_length=5) == "abcd…"
    assert WikiAgentService.title_from_query("   ") == "未命名"


def test_session_info_counts_only_visible_chat_messages() -> None:
    session = Session(key="session_test")
    session.history = [
        Message(role="user", content="问题"),
        Message(role="assistant", content="回答"),
        Message(role="assistant", content=""),
        Message(role="tool", content="内部工具结果"),
    ]

    info = WikiAgentService._to_info(session)

    assert info.message_count == 2
