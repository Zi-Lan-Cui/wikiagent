"""测试 memory/memory_strore.py — MemoryStore + Dreamer（user_id 级接口）"""

import json
from unittest.mock import MagicMock
from pathlib import Path
from wiki_agent.message import LLMResponse
from wiki_agent.session import Session
from wiki_agent.memory.memory_store import MemoryStore, Dreamer


UID = "u1"
SKEY = "s1"


# ═══════════════════════════════════════════
#  MemoryStore — 路径（改 user_id）
# ═══════════════════════════════════════════

class TestMemoryStorePaths:

    def test_history_file_path(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_history_file("u1") == tmp_path / "memory_store" / "history_files" / "u1" / "history.jsonl"

    def test_cursor_file_path(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_cursor_file("u1") == tmp_path / "memory_store" / "cursors" / "u1" / "cursor.txt"

    def test_dream_cursor_file_path(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_dream_cursor_file("u1") == tmp_path / "memory_store" / "dream_cursors" / "u1" / "dream_cursor.txt"

    def test_memory_file_path(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_memory_file("u1") == tmp_path / "memory_store" / "memory_files" / "u1" / "memory.md"

    def test_directories_created(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        ms.get_history_file("u1")
        ms.get_cursor_file("u1")
        ms.get_dream_cursor_file("u1")
        ms.get_memory_file("u1")
        assert (tmp_path / "memory_store" / "history_files" / "u1").is_dir()
        assert (tmp_path / "memory_store" / "cursors" / "u1").is_dir()
        assert (tmp_path / "memory_store" / "dream_cursors" / "u1").is_dir()
        assert (tmp_path / "memory_store" / "memory_files" / "u1").is_dir()


# ═══════════════════════════════════════════
#  MemoryStore — cursor
# ═══════════════════════════════════════════

class TestCursor:

    def test_get_cursor_nonexistent_returns_zero(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_cursor(UID) == 0

    def test_get_cursor_after_append(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        ms.append_history(s, summery="摘要A")
        assert ms.get_cursor(UID) == 1
        ms.append_history(s, summery="摘要B")
        assert ms.get_cursor(UID) == 2

    def test_get_cursor_negative_rebuilt(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        ms.append_history(s, summery="data")
        assert ms.get_cursor(UID) == 1

        cursor_file = ms.get_cursor_file(UID)
        cursor_file.write_text("-5")
        assert ms.get_cursor(UID) == 1

    def test_get_cursor_empty_file_rebuilt(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        ms.append_history(s, summery="data")
        ms.append_history(s, summery="data2")

        cursor_file = ms.get_cursor_file(UID)
        cursor_file.write_text("")
        assert ms.get_cursor(UID) == 2

    def test_cursor_persist_between_instances(self, tmp_path: Path):
        s = Session(key=SKEY, user_id=UID)
        ms1 = MemoryStore(workspace=tmp_path)
        ms1.append_history(s, summery="A")
        ms1.append_history(s, summery="B")

        ms2 = MemoryStore(workspace=tmp_path)
        assert ms2.get_cursor(UID) == 2


# ═══════════════════════════════════════════
#  MemoryStore — dream_cursor
# ═══════════════════════════════════════════

class TestDreamCursor:

    def test_get_zero_when_nonexistent(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_dream_cursor(UID) == 0

    def test_update_and_read(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        ms.update_dream_cursor(UID, new_cursor=5)
        assert ms.get_dream_cursor(UID) == 5

    def test_update_persist(self, tmp_path: Path):
        ms1 = MemoryStore(workspace=tmp_path)
        ms1.update_dream_cursor(UID, new_cursor=42)

        ms2 = MemoryStore(workspace=tmp_path)
        assert ms2.get_dream_cursor(UID) == 42

    def test_empty_file_yields_zero(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        dream_file = ms.get_dream_cursor_file(UID)
        dream_file.write_text("")
        assert ms.get_dream_cursor(UID) == 0


# ═══════════════════════════════════════════
#  append + unprocessed
# ═══════════════════════════════════════════

class TestAppendHistory:

    def test_record_cursor_one_based(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        ms.append_history(s, summery="用户问了天气")

        lines = ms.get_history_file(UID).read_text().strip().splitlines()
        record = json.loads(lines[0])
        assert record["cursor"] == 1
        assert record["session"] == SKEY
        assert record["summery"] == "用户问了天气"
        assert "time" in record

    def test_cursor_increments(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        ms.append_history(s, summery="A")
        ms.append_history(s, summery="B")
        ms.append_history(s, summery="C")

        records = _read_all_history(ms, UID)
        assert [r["cursor"] for r in records] == [1, 2, 3]


class TestGetUnprocessedHistory:

    def test_empty_when_no_history(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_unprocessed_history(UID) == {}

    def test_skips_processed(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        ms.append_history(s, summery="A")  # cursor=1
        ms.append_history(s, summery="B")  # cursor=2
        ms.append_history(s, summery="C")  # cursor=3

        ms.update_dream_cursor(UID, new_cursor=1)  # skip ≤1
        unprocessed = ms.get_unprocessed_history(UID)
        records = unprocessed[SKEY]
        assert len(records) == 2
        assert [r["summery"] for r in records] == ["B", "C"]

    def test_all_processed_empty(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        ms.append_history(s, summery="A")  # cursor=1
        ms.append_history(s, summery="B")  # cursor=2

        ms.update_dream_cursor(UID, new_cursor=2)
        assert ms.get_unprocessed_history(UID) == {}

    def test_grouped_by_session(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s_a = Session(key="a", user_id=UID)
        s_b = Session(key="b", user_id=UID)

        ms.append_history(s_a, summery="A: 天气")  # cursor=1
        ms.append_history(s_b, summery="B: 新闻")  # cursor=2
        ms.append_history(s_a, summery="A: 交通")  # cursor=3
        ms.append_history(s_b, summery="B: 体育")  # cursor=4

        unprocessed = ms.get_unprocessed_history(UID)
        assert set(unprocessed.keys()) == {"a", "b"}
        assert len(unprocessed["a"]) == 2
        assert len(unprocessed["b"]) == 2

    def test_cursor_skip_mixed_sessions(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key="s", user_id=UID)

        ms.append_history(s, summery="S1")                          # cursor=1
        ms.append_history(Session(key="other", user_id=UID), summery="OX")  # cursor=2
        ms.append_history(s, summery="S2")                          # cursor=3

        ms.update_dream_cursor(UID, new_cursor=1)
        unprocessed = ms.get_unprocessed_history(UID)
        assert set(unprocessed.keys()) == {"s", "other"}
        assert len(unprocessed["s"]) == 1
        assert unprocessed["s"][0]["summery"] == "S2"


# ═══════════════════════════════════════════
#  memory_text
# ═══════════════════════════════════════════

class TestMemoryText:

    def test_nonexistent_returns_empty(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        assert ms.get_memory_text(UID) == ""

    def test_read_existing(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        ms.get_memory_file(UID).write_text("用户喜欢钓鱼。")
        assert ms.get_memory_text(UID) == "用户喜欢钓鱼。"


# ═══════════════════════════════════════════
#  Dreamer
# ═══════════════════════════════════════════

class TestBuildDreamPrompt:

    def test_formats(self):
        d = Dreamer(workspace=Path("/tmp"), memory_store=MagicMock())
        prompt = d.build_dream_prompt(history="记录1", memory="用户画像")
        assert "记录1" in prompt
        assert "用户画像" in prompt
        assert "语言大师" in prompt

    def test_no_placeholder_left(self):
        d = Dreamer(workspace=Path("/tmp"), memory_store=MagicMock())
        prompt = d.build_dream_prompt(history="记录1", memory="")
        assert "{memory}" not in prompt


class TestDream:

    def test_no_history_noop(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        d = Dreamer(workspace=tmp_path, memory_store=ms)
        llm = MagicMock()

        d.dream(user_id=UID, llm=llm)
        llm.invoke.assert_not_called()

    def test_processes_unprocessed(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        d = Dreamer(workspace=tmp_path, memory_store=ms)
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(
            content="画像已更新：喜欢钓鱼，问了天气。",
            finish_reason="stop",
        )

        ms.append_history(s, summery="用户问了天气")
        ms.append_history(s, summery="用户问了钓鱼地点")
        assert ms.get_cursor(UID) == 2

        d.dream(user_id=UID, llm=llm)

        llm.invoke.assert_called_once()
        assert "钓鱼" in ms.get_memory_text(UID)
        assert ms.get_dream_cursor(UID) == 2

    def test_empty_llm_response_skips_write(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        d = Dreamer(workspace=tmp_path, memory_store=ms)
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(content="", finish_reason="stop")

        ms.append_history(s, summery="用户问了天气")
        d.dream(user_id=UID, llm=llm)

        assert ms.get_memory_text(UID) == ""
        assert ms.get_dream_cursor(UID) == 1

    def test_idempotent(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)
        d = Dreamer(workspace=tmp_path, memory_store=ms)
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(content="画像", finish_reason="stop")

        ms.append_history(s, summery="A")
        d.dream(user_id=UID, llm=llm)
        assert llm.invoke.call_count == 1

        # 无新 history
        d.dream(user_id=UID, llm=llm)
        assert llm.invoke.call_count == 1

    def test_cross_session_all_dreamed(self, tmp_path: Path):
        """一个 user_id 下所有 session 的未处理记录一起 dream"""
        ms = MemoryStore(workspace=tmp_path)
        s_a = Session(key="a", user_id=UID)
        s_b = Session(key="b", user_id=UID)

        ms.append_history(s_a, summery="A1")  # cursor=1
        ms.append_history(s_b, summery="B1")  # cursor=2
        ms.append_history(s_a, summery="A2")  # cursor=3

        d = Dreamer(workspace=tmp_path, memory_store=ms)
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(content="画像", finish_reason="stop")

        d.dream(user_id=UID, llm=llm)

        # session_a 和 session_b 各一次 LLM
        assert llm.invoke.call_count == 2
        assert ms.get_dream_cursor(UID) == 3

    def test_incremental_update(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        s = Session(key=SKEY, user_id=UID)

        ms.get_memory_file(UID).write_text("用户喜欢钓鱼。")

        d = Dreamer(workspace=tmp_path, memory_store=ms)
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(
            content="用户喜欢钓鱼，最近问过天气。",
            finish_reason="stop",
        )

        ms.append_history(s, summery="用户问了天气")
        d.dream(user_id=UID, llm=llm)

        prompt = llm.invoke.call_args[1]["messages"][0].content
        assert "用户喜欢钓鱼" in prompt  # old memory
        assert "cursor" in prompt         # history record
        assert "summery" in prompt

        updated = ms.get_memory_text(UID)
        assert "天气" in updated


class TestUpdateMemory:

    def test_atomic_write(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        d = Dreamer(workspace=tmp_path, memory_store=ms)
        d.update_memory(user_id=UID, update_content="新画像")
        assert ms.get_memory_text(UID) == "新画像"

    def test_overwrites(self, tmp_path: Path):
        ms = MemoryStore(workspace=tmp_path)
        d = Dreamer(workspace=tmp_path, memory_store=ms)
        d.update_memory(user_id=UID, update_content="v1")
        d.update_memory(user_id=UID, update_content="v2")
        assert ms.get_memory_text(UID) == "v2"


def _read_all_history(ms: MemoryStore, user_id: str) -> list[dict]:
    with open(ms.get_history_file(user_id)) as f:
        return [json.loads(line) for line in f if line.strip()]
