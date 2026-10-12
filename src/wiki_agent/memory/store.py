import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from wiki_agent.conversation import Session
from wiki_agent.log import get_logger
from wiki_agent.memory.items import MemoryEvent
from wiki_agent.utils import ensure_dir

logger = get_logger("MEMORY")


class MemoryStore:
    """会话记忆的文件存储：history.jsonl、事件流 memory.jsonl、游标文件与 memory.md 的读写。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        memory_dir = ensure_dir(self.workspace / "memory_store")
        if memory_dir is None:
            raise OSError(f"无法创建记忆目录: {self.workspace / 'memory_store'}")
        self.memory_dir: Path = memory_dir

    def _read_history_counts(self) -> int:
        count = 0
        try:
            with open(self.history_file) as f:
                for _ in f:
                    count += 1
                return count
        except (FileNotFoundError, ValueError):
            return 0

    @property
    def history_file(self) -> Path:
        return self.memory_dir / "history.jsonl"

    @property
    def cursor_file(self) -> Path:
        return self.memory_dir / "cursor.txt"

    @property
    def dream_cursor_file(self) -> Path:
        return self.memory_dir / "dream_cursor.txt"

    @property
    def memory_file(self) -> Path:
        return self.memory_dir / "memory.md"

    @property
    def events_file(self) -> Path:
        return self.memory_dir / "memory.jsonl"

    @property
    def rejected_file(self) -> Path:
        return self.memory_dir / "rejected.jsonl"

    @property
    def debug_file(self) -> Path:
        return self.memory_dir / "memory.debug.md"

    def get_cursor(self) -> int:
        """获取 history 游标（已处理的行数）。

        Returns:
            游标值；文件缺失或损坏时按 history 行数统计并回写校正。
        """
        try:
            raw = self.cursor_file.read_text()
            cursor = int(raw)

            if cursor <= 0:
                cursor = self._read_history_counts()
                self.update_cursor(cursor)
            return cursor
        except (FileNotFoundError, ValueError):
            return self._read_history_counts()

    def get_dream_cursor(self) -> int:
        """获取 dream 游标（该值之前的记录已被 Dreamer 处理）。

        Returns:
            dream 游标值；文件缺失或损坏返回 0。
        """
        try:
            raw = self.dream_cursor_file.read_text()
            if not raw:
                return 0
            return int(raw)
        except (FileNotFoundError, ValueError):
            return 0

    def _atomic_write(self, path: Path, content: str, fsync: bool = False) -> None:
        """原子写：写 tmp、可选 fsync、replace、目录 fsync。

        replace 使读者要么看到旧文件要么看到新文件；
        fsync 防止掉电丢失已确认的写入。

        Args:
            path: 目标文件路径。
            content: 写入内容。
            fsync: 是否强制刷盘（含目录）。
        """
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(content)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        if fsync:
            fd = os.open(path.parent, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)

    def update_cursor(self, new_cursor: int, fsync: bool = False):
        self._atomic_write(self.cursor_file, str(new_cursor), fsync=fsync)

    def read_events(self) -> list[MemoryEvent]:
        """逐行读 memory.jsonl 事件流；坏行跳过并告警——事件行是既成历史，不因一行损坏放弃整个 fold。"""
        try:
            raw = self.events_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        events: list[MemoryEvent] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                logger.warning("事件流坏行，跳过: %s", line[:120])
                continue
            if not isinstance(parsed, dict) or not parsed.get("event") or not parsed.get("id"):
                logger.warning("事件流缺 event/id，跳过: %s", line[:120])
                continue
            events.append(MemoryEvent.from_dict(parsed))
        return events

    def append_event(self, event: MemoryEvent, fsync: bool = False) -> None:
        """追加一条事件。O_APPEND 单行写短于一内核缓冲，读者不会读到半行。"""
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())

    def append_rejected(self, record: dict[str, Any]) -> None:
        """记录被校验拒绝的 op（含原始输出与原因），供人工审计幻觉处置。"""
        with open(self.rejected_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_projection(self, content: str, fsync: bool = False) -> None:
        """写 memory.md 投影——可删除可重建，不是真相源。"""
        self._atomic_write(self.memory_file, content, fsync=fsync)

    def write_debug_projection(self, content: str) -> None:
        """写 memory.debug.md 调试视图；仅 MEMORY_RENDER_DEBUG_MD 开启时调用。"""
        self._atomic_write(self.debug_file, content)

    def update_dream_cursor(self, new_cursor: int, fsync: bool = False):
        self._atomic_write(self.dream_cursor_file, str(new_cursor), fsync=fsync)

    def append_history(self, session: Session, summary: str, fsync: bool = False):
        """追加一条压缩记录。

        各会话的记录混写同一文件，由 get_unprocessed_history 按
        session.key 分组后交给 Dreamer。

        Args:
            session: 产生记录的会话。
            summary: 压缩摘要文本。
            fsync: 是否强制刷盘。
        """
        next_cursor = self.get_cursor() + 1
        with open(self.history_file, "a") as f:
            record = {
                "cursor": next_cursor,
                "time": datetime.now().isoformat(),
                "session": session.key,
                "summary": summary,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if fsync:
            fd = os.open(self.history_file.parent, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        self.update_cursor(next_cursor, fsync=fsync)

    def get_unprocessed_history(self) -> dict:
        """返回 dream 游标之后、按 session 分组的历史记录。

        Returns:
            session.key → 记录列表的映射；文件缺失或损坏返回空 dict。
        """
        memory_cursor = self.get_dream_cursor()
        try:
            group = defaultdict(list)
            with open(self.history_file) as f:
                for line in f:
                    entry = json.loads(line.strip())
                    # 兼容旧记录中的错拼字段 summery，读取时规范化
                    if "summary" not in entry:
                        entry["summary"] = entry.get("summery", "")
                    entry.pop("summery", None)
                    if int(entry["cursor"]) > memory_cursor:
                        group[entry["session"]].append(entry)
            return group
        except (FileNotFoundError, ValueError):
            return {}

    def get_memory_text(self) -> str:
        """读用户画像文本。

        Returns:
            memory.md 内容；文件缺失/损坏返回空串。
        """
        try:
            return self.memory_file.read_text(encoding="utf-8")
        except (FileNotFoundError, ValueError):
            return ""
