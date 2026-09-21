# 实现session和session manager

# session 主要负责单个会话的持久化和回复，消息检索存储，基本的元信息管理，不触碰数据本身的操作逻辑
# session manager 则负责管理一个会话的生命周期和状态管理，创建，读取，删除，获取状态等
import asyncio
import json
import os  # 用于将tmp替换原始文件
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

from wiki_agent.conversation.models import Message, find_first_legal_idx
from wiki_agent.log import get_logger
from wiki_agent.utils import (
    ensure_dir,
)

logger = get_logger("SESSION")


class Session:
    def __init__(self, key):
        # 身份/元信息
        self.key = key  # 我是谁，标识

        self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()

        # 状态信息
        self.session_title = "未命名"
        self.status: Literal["active", "closed"] = "active"
        self.token_cost: dict = {"prompt": 0, "completion": 0, "total": 0}
        self.current_window_tokens: int = 0
        self.last_consolidated: int = 0
        self.last_summary: str = ""
        # 已写入 MemoryStore.history 的会话消息边界，避免 idle 收尾
        # 在短会话上重复把同一批消息送进 Dreamer。
        self.last_memory_archived: int = 0

        # 内容信息
        self.history: list[Message] = []

    def add_message(self, message: Message):
        self.history.append(message)

        self.updated_at = datetime.now().isoformat()

    def add_messages(self, messages: list[Message]):
        self.history.extend(messages)

        self.updated_at = datetime.now().isoformat()

    def get_history(self, max_messages_length: int = 10, extend_to_user: bool = True):
        """
        返回满足最大长度且起始合法的历史窗口。

        只保证窗口起点合法，不保证整个窗口合法（build 阶段再处理）。

        Args:
            max_messages_length: 窗口最大消息数。
            extend_to_user: 为 True 时窗口首条消息必须是 user。

        Returns:
            截取后的消息列表（空窗口返回 []）。
        """
        if max_messages_length <= 0:
            return []
        # 1. 选中可选的未压缩信息
        unconsolidated_messages = self.history[self.last_consolidated :]
        limited_messages = []
        if len(unconsolidated_messages) < max_messages_length:
            limited_messages = unconsolidated_messages
        else:
            limited_messages = unconsolidated_messages[-max_messages_length:]
        start = find_first_legal_idx(limited_messages, extend_to_user)
        messages = limited_messages[start:]
        return messages

    def update_token_cost(self, prompt: int, completion: int, total: int):
        """
        累加本轮调用的 token 消耗。

        Args:
            prompt: 本轮 prompt token 数。
            completion: 本轮生成 token 数。
            total: 本轮总 token 数。
        """
        self.token_cost["prompt"] += prompt
        self.token_cost["completion"] += completion
        self.token_cost["total"] += total


class SessionManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        sessions_dir = ensure_dir(self.workspace / "sessions")
        if sessions_dir is None:
            raise OSError(f"无法创建会话目录: {self.workspace / 'sessions'}")
        self.sessions_dir: Path = sessions_dir
        self._cached_session = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def session_lock(self, session_key: str):
        """串行化同一 session 的完整 Agent turn。"""
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def get_or_create(self, session_key: str) -> Session:
        if session_key in self._cached_session:
            return self._cached_session[session_key]

        session = self._load(session_key)

        if session is None:
            session = Session(key=session_key)

        self._cached_session[session_key] = session
        return session

    def cached_sessions(self) -> list[Session]:
        """返回当前进程已加载的会话，供 idle watcher 检查。"""
        return list(self._cached_session.values())

    def _prase_checkpoint(self, file_path: Path):
        metadata: dict = {}
        history = []
        with open(file_path) as f:
            for line in f:
                try:
                    # 处理空行,空行会带"\n"，需要进行strip
                    if not line.strip():
                        continue

                    line_data = json.loads(line)
                    if "_type" in line_data:
                        metadata.update(line_data)
                        continue
                    else:
                        history.append(Message.model_validate(line_data))
                except Exception as e:
                    logger.warning(f"数据 {line[:50]} 处理失败，将被跳过 - {e}")
                    continue
        return metadata, history

    def _validate_meta_data(self, meta_data: dict):
        # key/last_consolidated 用 is None 判断（0 是合法值——空会话游标从 0 开始）
        for required in ("key", "last_consolidated"):
            if meta_data.get(required, None) is None:
                logger.warning("元信息损坏（缺 %s），将新建会话", required)
                return {}

        if not (meta_data.get("created_at") and meta_data.get("updated_at")):
            now = datetime.now().isoformat()
            meta_data["created_at"] = meta_data["updated_at"] = now

        if not meta_data.get("session_title", None):
            meta_data["session_title"] = "未命名"

        if not meta_data.get("status", None):
            meta_data["status"] = "active"

        # 兼容旧 checkpoint 的错拼字段；后续 save_checkpoint 只写新字段。
        if meta_data.get("last_summary") is None:
            meta_data["last_summary"] = meta_data.get("last_summery", "")

        if meta_data.get("current_window_tokens", None) is None:
            meta_data["current_window_tokens"] = 0

        if meta_data.get("token_cost", None) is None:
            logger.warning("token cost信息损坏，将从0计费")
            meta_data["token_cost"] = {"prompt": 0, "completion": 0, "total": 0}

        return meta_data

    def _load(self, session_key):
        file_path = self.sessions_dir / f"{session_key}.jsonl"
        if not file_path.exists():
            return None
        else:
            meta_data, history = self._prase_checkpoint(file_path)

            # 分解文件，读取metadata，和content，重新组织session
            validate_meta_data = self._validate_meta_data(meta_data)
            if validate_meta_data:
                session = Session(key=meta_data["key"])
                session.history = history
                session.status = validate_meta_data["status"]
                session.created_at = validate_meta_data["created_at"]
                session.updated_at = validate_meta_data["updated_at"]
                session.last_consolidated = validate_meta_data["last_consolidated"]
                session.session_title = validate_meta_data["session_title"]
                session.last_summary = validate_meta_data["last_summary"]
                session.last_memory_archived = validate_meta_data.get("last_memory_archived", 0)
                session.token_cost = validate_meta_data["token_cost"]
                session.current_window_tokens = validate_meta_data["current_window_tokens"]
                return session
        return None

    def save_checkpoint(self, session: Session, fsync: bool = False):
        """
        将会话持久化到磁盘（临时文件 + 原子替换）。

        fsync 为 True 时立即刷入磁盘（较慢）；False（默认）只写
        操作系统页缓存——关机自动落盘，但掉电可能丢失。

        Args:
            session: 要保存的会话。
            fsync: 是否强制 fsync 刷盘。

        Returns:
            True 表示保存成功；False 表示失败（sessions 目录不可用
            或写入异常），调用方需自行处理。
        """

        key = session.key
        session_dir = ensure_dir(self.sessions_dir)
        if session_dir is not None:
            file_path = session_dir / f"{key}.jsonl"
            # 先创建临时文件，再写入
            tmp_file_path = file_path.with_suffix(".tmp")
            try:
                with open(tmp_file_path, "w", encoding="utf-8") as f:
                    metadata_line = {
                        "_type": "metadata",
                        "key": session.key,
                        "created_at": session.created_at,
                        "updated_at": session.updated_at,
                        "session_title": session.session_title,
                        "last_consolidated": session.last_consolidated,
                        "last_summary": session.last_summary,
                        "last_memory_archived": session.last_memory_archived,
                        "status": session.status,
                        "token_cost": session.token_cost,
                        "current_window_tokens": session.current_window_tokens,
                    }
                    f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")

                    for message in session.history:
                        # message还原成字典格式再写入,加\n换行
                        f.write(message.model_dump_json() + "\n")

                    # 立即同步到磁盘
                    if fsync:
                        f.flush()
                        os.fsync(f.fileno())

                os.replace(tmp_file_path, file_path)

                # 目录本质上是特殊的文件，目录交换的改变也会先被缓存，必须对目录也强制刷新

                if fsync:
                    with suppress(PermissionError):
                        # 注意目录是.parent
                        fd = os.open(str(file_path.parent), os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
            except BaseException as e:
                # 删除临时文件，missing_ok表示允许文件不存在
                tmp_file_path.unlink(missing_ok=True)
                # 不用 logger.exception——全栈回溯刷到终端是噪音
                # （会话保存在此失败是预期可恢复路径：返回 False，
                # 调用方知道没落盘）。异常对象本身已带足够诊断信息。
                logger.error(
                    "会话保存失败 [%s]: %s: %s", session.key, type(e).__name__, str(e)[:200]
                )
                return False
        else:
            # sessions 路径被文件占位——正常运行时不会发生（初始化即建目录），
            # 防御性返回 False，不崩（UnboundLocalError 事故）
            logger.error("sessions 目录不可用: %s", self.sessions_dir)
            return False

        # 刷新缓存的session，如果没缓存，这行会将其加入到缓存
        self._cached_session[key] = session

        # 检查并返回文件是否保存成功
        return True

    def get_session_token_cost(
        self,
        session_key: str,
    ):
        session = self.get_or_create(session_key=session_key)
        return session.token_cost

    def list_session_keys(self) -> list[str]:
        """列出磁盘上所有 session key，按修改时间倒序。

        Returns:
            session key 列表（sessions 目录不存在时返回空列表）。
        """
        if not self.sessions_dir.is_dir():
            return []
        files = sorted(
            (p for p in self.sessions_dir.iterdir() if p.suffix == ".jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [p.stem for p in files]
