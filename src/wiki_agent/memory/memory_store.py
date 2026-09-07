import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from wiki_agent.llm import LLMClient
from wiki_agent.log import get_logger
from wiki_agent.message import Message
from wiki_agent.session import Session
from wiki_agent.utils import helpers

# 单用户模式：所有 session 共享同一个 history 文件，按 session.key 分组压缩更新 memory。

# cursor 和 dream cursor 分别记录 history 长度和压缩进度。

# 画像管道: append_history → Dreamer.dream（LLM 加工）→ memory.md。
# Wiki 纠错属于问题域，由 IssueStore 持久化。

logger = get_logger("MEMORY")


class MemoryStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        memory_dir = helpers.ensure_dir(self.workspace / "memory_store")
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

    # ── 文件路径 (properties) ─────────────────────────────────

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

    # ── cursor 读写 ──────────────────────────────────────────

    def get_cursor(self) -> int:
        """
        获取 history 游标（已处理的行数）。

        Returns:
            游标值；文件缺失/损坏时回退为按文件行数统计
            （并回写校正游标文件）。
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
        """
        获取 dream 游标（指向的值已被处理）。

        Returns:
            dream 游标值；文件缺失/损坏返回 0。
        """
        try:
            raw = self.dream_cursor_file.read_text()
            if not raw:
                return 0
            return int(raw)
        except (FileNotFoundError, ValueError):
            return 0

    def _atomic_write(self, path: Path, content: str, fsync: bool = False) -> None:
        """原子写——tmp + fsync（可选）+ replace + 目录 fsync。

        与 session.save_checkpoint 同模式: replace 保证读者
        要么旧要么新；fsync 防掉电丢已确认的写。

        Args:
            path: 目标文件路径。
            content: 写入内容。
            fsync: 是否强制刷盘（目录 fsync 一并做）。
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

    # ── history 读写 ─────────────────────────────────────────

    def append_history(self, session: Session, summary: str, fsync: bool = False):
        """
        追加一条压缩记录。

        不区分 session，按 session.key 分组后再由 dreamer 处理。

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
        """
        返回 dream 游标之后按 session 分组的历史记录。

        Returns:
            session.key → 记录列表的映射；文件缺失/损坏返回空 dict。
        """
        memory_cursor = self.get_dream_cursor()
        try:
            group = defaultdict(list)
            with open(self.history_file) as f:
                for line in f:
                    entry = json.loads(line.strip())
                    # history.jsonl 曾将 summary 错拼为 summery；读取时
                    # 规范化，下一次追加只会写正确字段。
                    if "summary" not in entry:
                        entry["summary"] = entry.get("summery", "")
                    entry.pop("summery", None)
                    if int(entry["cursor"]) > memory_cursor:
                        group[entry["session"]].append(entry)
            return group
        except (FileNotFoundError, ValueError):
            return {}

    # ── memory 读写 ──────────────────────────────────────────

    def get_memory_text(self) -> str:
        """读用户画像文本。

        Returns:
            memory.md 内容；文件缺失/损坏返回空串。
        """
        try:
            return self.memory_file.read_text(encoding="utf-8")
        except (FileNotFoundError, ValueError):
            return ""


class Dreamer:
    _DREAM_PROMPT = """
        你是一名语言大师，擅长根据记录更新用户的画像，分析用户的爱好，价值观等能描述用户的信息，
        并根据已有的记录信息，对记录进行完整的重写，新的重写涵盖更丰富完整的用户描述。

        # 注意
        1. 不能简单增加文字，而是进行汇总，重新整理
        2. 只更新与用户相关内容，不关心助手
        3. 如果记录中记录了无关信息，则在更新中移除

        需要处理的历史:
        {history}

        已有的描述：
        {memory}
    """

    def __init__(
        self,
        workspace: Path,
        memory_store: MemoryStore,
    ):
        self.workspace = workspace
        self.memory_store = memory_store

    def build_dream_prompt(self, history: str, memory: str) -> str:
        """组装 dream 提示词。

        Args:
            history: 待处理历史文本。
            memory: 现有用户画像。

        Returns:
            格式化后的提示词文本。
        """
        return self._DREAM_PROMPT.format(history=history, memory=memory)

    async def dream(self, llm: LLMClient):
        """单用户 dream——获取所有未处理的历史，更新 memory。

        Args:
            llm: LLM 客户端（生成更新后的画像）。
        """
        grouped_history = self.memory_store.get_unprocessed_history()
        memory = self.memory_store.get_memory_text()
        new_cursor = self.memory_store.get_cursor()

        failed = False
        for session_key, history in grouped_history.items():
            # 逐行拼接（换行分隔）——连续 JSON 对象粘连会让 LLM
            # 解析靠自然能力猜边界；每行一条是 JSONL 本来的形态
            text_history = "\n".join([json.dumps(record) for record in history])

            update_messages = [
                Message(
                    role="system",
                    content=self.build_dream_prompt(history=text_history, memory=memory),
                )
            ]

            try:
                response = await llm.async_invoke(messages=update_messages)
            except Exception as exc:
                failed = True
                logger.warning(
                    "session %s Dream失败: %s: %s", session_key, type(exc).__name__, str(exc)[:160]
                )
                continue

            if response.content:
                self.update_memory(update_content=response.content)
            else:
                failed = True
                logger.warning(f"session {session_key} Dream返回结果为空，跳过更新")

        # 任一 session 更新失败都不能推进全局游标，否则失败记录会被
        # 永久跳过，下一轮无法恢复。
        if not failed:
            self.memory_store.update_dream_cursor(new_cursor=new_cursor)

    def update_memory(self, update_content: str, fsync: bool = False):
        """写入更新后的用户画像（memory.md）。

        Args:
            update_content: 新画像文本。
            fsync: 是否强制刷盘。
        """
        self.memory_store._atomic_write(self.memory_store.memory_file, update_content, fsync=fsync)
