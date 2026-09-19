"""Session 模型 + 持久化测试。

覆盖: 消息管理 / history 窗口与合法起始 / checkpoint 往返 /
压缩游标跨轮恢复 / manager 缓存与异常路径。

直接运行:  .venv/bin/python test/test_session.py
pytest 运行: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest test/test_session.py
"""

import asyncio
import json
import tempfile
from pathlib import Path

from wiki_agent.conversation import Message, Session, SessionManager
from wiki_agent.memory import MemoryStore


def test_session_init_defaults():
    s = Session(key="k1")
    assert s.key == "k1"
    assert s.history == []
    assert s.status == "active"
    assert s.last_consolidated == 0
    assert s.last_summary == ""
    assert s.current_window_tokens == 0
    assert s.token_cost == {"prompt": 0, "completion": 0, "total": 0}
    assert s.last_memory_archived == 0


def test_add_messages_bulk_and_timestamp():
    s = Session(key="k1")
    old = s.updated_at
    s.add_messages(
        [
            Message(role="user", content="a"),
            Message(role="assistant", content="b"),
        ]
    )
    assert len(s.history) == 2
    assert s.updated_at != old


def test_get_history_window_and_legal_start():
    """窗口截取 + 起始必须合法（user 开头，tool 孤儿前移）。"""
    s = Session(key="k1")
    for i in range(5):
        s.add_messages(
            [
                Message(role="user", content=f"q{i}"),
                Message(role="assistant", content=f"a{i}"),
            ]
        )
    # 窗口 3 条：取最近 3 = [a4? 不对——序列 q0 a0 q1 a1 ...，最近3 = a4 之后？]
    # 序列: q0 a0 q1 a1 q2 a2 q3 a3 q4 a4；最近3 = q4? 不——[a3, q4, a4]? 直接验证语义:
    result = s.get_history(max_messages_length=3)
    # 起始必须 user：最近的 3 条是 [a3, q4, a4]？——不，最近3 = [q4, a4]...
    # 实现: unconsolidated[-3:] = [a3, q4, a4]；find_first_legal_idx 从 a3 开始
    # 前移到 q4。所以 result = [q4, a4]。
    assert [m.role for m in result] == ["user", "assistant"]
    assert result[0].content == "q4"


def test_history_respects_last_consolidated():
    """压缩游标前的内容不出现在 history 窗口（已进摘要）。"""
    s = Session(key="k1")
    for i in range(4):
        s.add_messages(
            [
                Message(role="user", content=f"q{i}"),
                Message(role="assistant", content=f"a{i}"),
            ]
        )
    s.last_consolidated = 6  # 前 6 条已压缩
    result = s.get_history(max_messages_length=10)
    assert [m.content for m in result] == ["q3", "a3"]


# checkpoint 往返


def _make_manager(tmp: Path) -> SessionManager:
    return SessionManager(workspace=tmp)


def test_save_load_roundtrip():
    tmp = Path(tempfile.mkdtemp())
    sm = _make_manager(tmp)
    s = sm.get_or_create("k1")
    s.add_messages(
        [
            Message(role="user", content="q1"),
            Message(role="assistant", content="a1"),
        ]
    )
    s.last_consolidated = 1
    s.last_summary = "摘要"
    s.token_cost = {"prompt": 100, "completion": 50, "total": 150}
    s.current_window_tokens = 999
    assert sm.save_checkpoint(s) is True

    sm2 = SessionManager(workspace=tmp)  # 新实例，绕过缓存
    s2 = sm2.get_or_create("k1")
    assert [m.content for m in s2.history] == ["q1", "a1"]
    assert s2.last_consolidated == 1
    assert s2.last_summary == "摘要"
    assert s2.token_cost == {"prompt": 100, "completion": 50, "total": 150}
    assert s2.current_window_tokens == 999


def test_legacy_session_summary_is_loaded_and_rewritten_with_new_name():
    tmp = Path(tempfile.mkdtemp())
    sessions = tmp / "sessions"
    sessions.mkdir()
    checkpoint = sessions / "legacy.jsonl"
    checkpoint.write_text(
        json.dumps(
            {
                "_type": "metadata",
                "key": "legacy",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "last_consolidated": 0,
                "last_summery": "旧摘要",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manager = _make_manager(tmp)
    session = manager.get_or_create("legacy")
    assert session.last_summary == "旧摘要"
    assert manager.save_checkpoint(session)
    saved = checkpoint.read_text(encoding="utf-8")
    assert '"last_summary": "旧摘要"' in saved
    assert "last_summery" not in saved


def test_legacy_memory_history_summary_is_normalized_on_read():
    tmp = Path(tempfile.mkdtemp())
    store = MemoryStore(workspace=tmp)
    store.history_file.write_text(
        json.dumps({"cursor": 1, "session": "legacy", "summery": "旧历史摘要"}) + "\n",
        encoding="utf-8",
    )

    records = store.get_unprocessed_history()["legacy"]
    assert records == [{"cursor": 1, "session": "legacy", "summary": "旧历史摘要"}]
    store.append_history(Session("new"), summary="新历史摘要")
    saved = store.history_file.read_text(encoding="utf-8").splitlines()[-1]
    assert '"summary": "新历史摘要"' in saved
    assert "summery" not in saved


def test_load_nonexistent_returns_none_via_new_session():
    """不存在 → 新 session（get_or_create 语义）。"""
    tmp = Path(tempfile.mkdtemp())
    sm = _make_manager(tmp)
    s = sm.get_or_create("ghost")
    assert s.history == []
    assert s.key == "ghost"


def test_session_lock_serializes_same_key():
    sm = SessionManager(workspace=Path(tempfile.mkdtemp()))
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with sm.session_lock("same"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    async def scenario():
        await asyncio.gather(worker(), worker())

    asyncio.run(scenario())
    assert peak == 1


def test_load_corrupt_lines_skipped():
    """坏行跳过，好行保留。"""
    tmp = Path(tempfile.mkdtemp())
    sm = _make_manager(tmp)
    s = sm.get_or_create("k1")
    s.add_message(Message(role="user", content="ok"))
    sm.save_checkpoint(s)
    # 追加一行垃圾
    with open(tmp / "sessions" / "k1.jsonl", "a", encoding="utf-8") as f:
        f.write("{corrupt json\n")
    sm2 = SessionManager(workspace=tmp)
    s2 = sm2.get_or_create("k1")
    assert [m.content for m in s2.history] == ["ok"]


def test_save_fails_gracefully_no_crash():
    """保存异常不冒泡（返回 False），不破坏已缓存 session。"""
    tmp = Path(tempfile.mkdtemp())
    sm = _make_manager(tmp)
    s = sm.get_or_create("k1")
    s.add_message(Message(role="user", content="hi"))
    # 把 sessions 目录换成只读文件，让 save 打开失败
    import shutil

    shutil.rmtree(tmp / "sessions")
    (tmp / "sessions").write_text("占位", encoding="utf-8")
    assert sm.save_checkpoint(s) is False


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
