import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from wiki_agent.conversation import Session
from wiki_agent.utils import ensure_dir


class MemoryStore:
    """会话记忆的文件存储：history.jsonl、游标文件与 memory.md 的读写。"""

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
