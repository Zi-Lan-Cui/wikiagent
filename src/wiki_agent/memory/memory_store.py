from pathlib import Path
from datetime import datetime
from collections import defaultdict

from wiki_agent.session import Session
from wiki_agent.utils import helpers
from wiki_agent.message import Message
from wiki_agent.llm import LLMClient
from wiki_agent.log import get_logger

import json
import os

# 单用户模式：所有 session 共享同一个 history 文件，按 session.key 分组压缩更新 memory。

# cursor 和 dream cursor 分别记录 history 长度和压缩进度。

# ── 两条落账管道（互不交叉）──────────────────────────────────
# 1. 画像管道:   append_history → Dreamer.dream（LLM 加工）→ memory.md
#    对话摘要是"画像原料"——冗长重复，需要 LLM 汇总重写。
# 2. 纠错管道:   append_correction → corrections.md（代码直接追加）
#    wiki 纠错是"终态事实"——自然语言一句话，不需要加工；
#    再让 LLM 重写是风险（丢页面名/细节）不是收益。
#    corrections.md 是待修清单，消费端（refine/surgery 前）读取。

logger=get_logger("MEMORY")

class MemoryStore:
    def __init__(self,workspace:Path):
        self.workspace=workspace
        self.memory_dir=helpers.ensure_dir(self.workspace/"memory_store")

    def _read_history_counts(self)->int:
        count=0
        try:
            with open(self.history_file,"r") as f:
                for _ in f:
                    count+=1
                return count
        except (FileNotFoundError,ValueError):
            return 0

    # ── 文件路径 (properties) ─────────────────────────────────

    @property
    def history_file(self)->Path:
        return self.memory_dir/"history.jsonl"

    @property
    def cursor_file(self)->Path:
        return self.memory_dir/"cursor.txt"

    @property
    def dream_cursor_file(self)->Path:
        return self.memory_dir/"dream_cursor.txt"

    @property
    def memory_file(self)->Path:
        return self.memory_dir/"memory.md"

    @property
    def corrections_file(self)->Path:
        """wiki 纠错待修清单——自然语言原样落账（不做结构化）。"""
        return self.memory_dir/"corrections.md"

    # ── cursor 读写 ──────────────────────────────────────────

    def get_cursor(self)->int:
        """
        获取 history 游标（已处理的行数）。

        Returns:
            游标值；文件缺失/损坏时回退为按文件行数统计
            （并回写校正游标文件）。
        """
        try:
            raw=self.cursor_file.read_text()
            cursor=int(raw)

            if cursor <= 0:
                cursor=self._read_history_counts()
                self.update_cursor(cursor)
            return cursor
        except (FileNotFoundError,ValueError):
            return self._read_history_counts()

    def get_dream_cursor(self)->int:
        """
        获取 dream 游标（指向的值已被处理）。

        Returns:
            dream 游标值；文件缺失/损坏返回 0。
        """
        try:
            raw=self.dream_cursor_file.read_text()
            if not raw:
                return 0
            return int(raw)
        except (FileNotFoundError,ValueError):
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

    def update_cursor(self,new_cursor:int,fsync:bool=False):
        self._atomic_write(self.cursor_file, str(new_cursor), fsync=fsync)

    def update_dream_cursor(self,new_cursor:int,fsync:bool=False):
        self._atomic_write(self.dream_cursor_file, str(new_cursor), fsync=fsync)

    # ── history 读写 ─────────────────────────────────────────

    def append_history(
            self,
            session:Session,
            summery:str,
            fsync:bool=False
    ):
        """
        追加一条压缩记录。

        不区分 session，按 session.key 分组后再由 dreamer 处理。

        Args:
            session: 产生记录的会话。
            summery: 压缩摘要文本。
            fsync: 是否强制刷盘。
        """
        next_cursor=self.get_cursor()+1
        with open(self.history_file,"a") as f:
            record={
                "cursor":next_cursor,
                "time":datetime.now().isoformat(),
                "session":session.key,
                "summery":summery
            }
            f.write(json.dumps(record,ensure_ascii=False)+"\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if fsync:
            fd=os.open(self.history_file.parent,os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        self.update_cursor(next_cursor,fsync=fsync)

    def get_unprocessed_history(self)->dict:
        """
        返回 dream 游标之后按 session 分组的历史记录。

        Returns:
            session.key → 记录列表的映射；文件缺失/损坏返回空 dict。
        """
        memory_cursor=self.get_dream_cursor()
        try:
            group=defaultdict(list)
            with open(self.history_file,"r") as f:
                for line in f:
                    entry=json.loads(line.strip())
                    if  int(entry["cursor"]) > memory_cursor:
                        group[entry["session"]].append(entry)
            return group
        except (FileNotFoundError,ValueError):
            return {}

    # ── memory 读写 ──────────────────────────────────────────

    def get_memory_text(self)->str:
        """读用户画像文本。

        Returns:
            memory.md 内容；文件缺失/损坏返回空串。
        """
        try:
            return self.memory_file.read_text(encoding="utf-8")
        except (FileNotFoundError,ValueError):
            return ""

    # ── 纠错清单（独立管道，不经 Dreamer）────────────────────

    def append_correction(
            self,
            text:str,
            session_key:str="",
            fsync:bool=False
    ):
        """追加一条 wiki 纠错——自然语言原样落账。

        text 是 LLM 用自然语言总结的纠错事实（"该页闭包示例有误"），
        不做结构化、不再 LLM 加工——corrections.md 就是待修清单。
        调用点: /fix 命令或 hook 捕获（即时落账，不等 dream 周期）。

        Args:
            text: 纠错内容（自然语言）。
            session_key: 来源会话标识（可空）。
            fsync: 是否强制刷盘。
        """
        line = (
            f"- [{datetime.now().isoformat()}]"
            + (f" [{session_key}]" if session_key else "")
            + f" {text.strip()}\n"
        )
        with open(self.corrections_file, "a", encoding="utf-8") as f:
            f.write(line)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if fsync:
            fd = os.open(self.corrections_file.parent, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)

    def get_corrections(self)->list[str]:
        """读取待修清单。

        Returns:
            逐条纠错文本列表；文件缺失/损坏返回空列表。
        """
        try:
            return [
                line for line in
                self.corrections_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (FileNotFoundError,ValueError):
            return []

    # ── 裁决操作（/resolve 用）──────────────────────────────

    def _rewrite_corrections(self, lines: list[str]) -> None:
        """整写 corrections.md——条目量小，重写即事务。

        Args:
            lines: 要写入的行列表。
        """
        with open(self.corrections_file, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))

    def remove_correction(self, index: int) -> bool:
        """移除第 index 条（0-based）——驳回语义（wiki 对，用户观点弃）。

        Args:
            index: 条目下标（0-based）。

        Returns:
            True 表示移除成功；下标越界返回 False。
        """
        lines = self.get_corrections()
        if not (0 <= index < len(lines)):
            return False
        removed = lines.pop(index)
        self._rewrite_corrections(lines)
        logger.info("纠错驳回 [%d]: %s", index, removed[:60])
        return True

    def mark_correction(self, index: int, marker: str) -> bool:
        """给第 index 条加状态标记（行首）——accept/keep 语义。

        幂等（已带标记则替换）。

        Args:
            index: 条目下标（0-based）。
            marker: "[已确认待修]" / "[存疑]" 等；空串清除标记。

        Returns:
            True 表示操作成功；下标越界返回 False。
        """
        lines = self.get_corrections()
        if not (0 <= index < len(lines)):
            return False
        line = lines[index]
        # 去掉旧标记再贴新标记——幂等重标记
        for known in ("[已确认待修] ", "[存疑] "):
            line = line.replace(known, "")
        lines[index] = f"{marker} {line}" if marker else line
        self._rewrite_corrections(lines)
        logger.info("纠错标记 [%d]: %s", index, marker)
        return True


class Dreamer:

    _DREAM_PROMPT="""
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
            workspace:Path,
            memory_store:MemoryStore,
        ):
        self.workspace=workspace
        self.memory_store=memory_store

    def build_dream_prompt(
            self,
            history:str,
            memory:str
    )->str:
        """组装 dream 提示词。

        Args:
            history: 待处理历史文本。
            memory: 现有用户画像。

        Returns:
            格式化后的提示词文本。
        """
        return self._DREAM_PROMPT.format(
            history=history,
            memory=memory
        )

    async def dream(self,llm:LLMClient):
        """单用户 dream——获取所有未处理的历史，更新 memory。

        Args:
            llm: LLM 客户端（生成更新后的画像）。
        """
        grouped_history=self.memory_store.get_unprocessed_history()
        memory=self.memory_store.get_memory_text()
        new_cursor=self.memory_store.get_cursor()

        for session_key, history in grouped_history.items():
            # 逐行拼接（换行分隔）——连续 JSON 对象粘连会让 LLM
            # 解析靠自然能力猜边界；每行一条是 JSONL 本来的形态
            text_history="\n".join([json.dumps(record) for record in history])

            update_messages=[
                Message(
                    role="system",
                    content=self.build_dream_prompt(
                        history=text_history,
                        memory=memory
                    )
                )
            ]

            response= await llm.async_invoke(messages=update_messages)

            if response.content:
                self.update_memory(update_content=response.content)
            else:
                logger.warning(f"session {session_key} Dream返回结果为空，跳过更新")

        self.memory_store.update_dream_cursor(new_cursor=new_cursor)

    def update_memory(
            self,
            update_content:str,
            fsync:bool=False
    ):
        """写入更新后的用户画像（memory.md）。

        Args:
            update_content: 新画像文本。
            fsync: 是否强制刷盘。
        """
        self.memory_store._atomic_write(
            self.memory_store.memory_file, update_content, fsync=fsync)
