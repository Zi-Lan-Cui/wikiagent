"""会话与会话管理。

Session 负责单个会话的持久化、消息存储检索与元信息；
SessionManager 负责会话生命周期：创建、读取、删除、状态查询。
"""

import asyncio
import json
import os  # 用于原子替换与 fsync
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from wiki_agent.conversation.models import Message, find_first_legal_idx
from wiki_agent.log import get_logger
from wiki_agent.utils import (
    ensure_dir,
)

logger = get_logger("SESSION")

_FREE = asyncio.Lock()  # 会话无锁时的占位，供 locked() 判断


class Session:
    def __init__(self, key):
        self.key = key

        self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()

        self.session_title = "未命名"
        self.status: Literal["active", "closed"] = "active"
        self.token_cost: dict = {"prompt": 0, "completion": 0, "total": 0}
        self.current_window_tokens: int = 0
        self.last_consolidated: int = 0
        self.last_summary: str = ""
        # 已写入 MemoryStore.history 的消息边界，idle 收尾据此
        # 避免把同一批消息重复送进 Dreamer
        self.last_memory_archived: int = 0

        self.history: list[Message] = []
        # 已落盘消息条数；history 只追加，压缩仅移动游标
        self._persisted_count = 0

    def add_message(self, message: Message):
        self.history.append(message)

        self.updated_at = datetime.now().isoformat()

    def add_messages(self, messages: list[Message]):
        self.history.extend(messages)

        self.updated_at = datetime.now().isoformat()

    def get_history(self, max_messages_length: int = 10, extend_to_user: bool = True):
        """返回满足最大长度且起点合法的历史窗口。

        只保证窗口起点合法，不保证整个窗口合法。

        Args:
            max_messages_length: 窗口最大消息数。
            extend_to_user: 为 True 时窗口首条消息必须是 user。

        Returns:
            截取后的消息列表（空窗口返回 []）。
        """
        if max_messages_length <= 0:
            return []
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
        """累加本轮调用的 token 消耗。

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
        self._cached_session: dict[str, Session] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._cache_limit = 128

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

        # 长驻进程防单调增长：超上限按插入序逐出最早的未持锁会话
        while len(self._cached_session) >= self._cache_limit:
            evict = next(
                (
                    k
                    for k in self._cached_session
                    if k != session_key and not self._session_locks.get(k, _FREE).locked()
                ),
                None,
            )
            if evict is None:
                break
            self._cached_session.pop(evict, None)
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
        # 用 is None 判断：0 是合法值（空会话游标从 0 开始）
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

        # 兼容旧 checkpoint 中的错拼字段 last_summery
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
            meta_path = self.sessions_dir / f"{session_key}.meta.json"
            if meta_path.is_file():
                try:
                    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    logger.warning("meta 文件损坏，回退 jsonl 内嵌头: %s", session_key)

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
                session._persisted_count = len(history)
                return session
        return None

    async def asave(self, session: Session, *, fsync: bool = False) -> bool:
        """在线程池中调用 save_checkpoint。"""
        return await asyncio.to_thread(self.save_checkpoint, session, fsync)

    def save_checkpoint(self, session: Session, fsync: bool = False):
        """持久化会话：meta 原子替换小文件，消息增量追加。

        history 只追加（压缩只移动游标，不删行），每次只写上次落盘后的
        新增行，写盘成本与会话总长无关。异常退出留下的半行由读取侧跳过，
        载入后按现存行数对齐 _persisted_count。

        fsync 为 True 时立即刷盘（较慢）；False 只写页缓存。

        Args:
            session: 要保存的会话。
            fsync: 是否强制刷盘。

        Returns:
            True 保存成功；False 目录不可用或写失败，调用方自行处理。
        """
        session_dir = ensure_dir(self.sessions_dir)
        if session_dir is None:
            return False
        key = session.key
        file_path = session_dir / f"{key}.jsonl"
        metadata_line = {
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
        try:
            new_messages = session.history[session._persisted_count:]
            if new_messages or not file_path.exists():
                with open(file_path, "a", encoding="utf-8") as f:
                    for message in new_messages:
                        f.write(message.model_dump_json() + "\n")
                    if fsync:
                        f.flush()
                        os.fsync(f.fileno())
            # meta 在消息之后写：其 updated_at 不早于本轮消息内容
            tmp_meta = file_path.with_suffix(".meta.tmp")
            with open(tmp_meta, "w", encoding="utf-8") as f:
                f.write(json.dumps(metadata_line, ensure_ascii=False))
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp_meta, session_dir / f"{key}.meta.json")
            if fsync:
                # 刷新目录，保证替换后的 meta 可见
                with open(session_dir) as dir_fd:
                    os.fsync(dir_fd.fileno())
            session._persisted_count = len(session.history)
            return True
        except OSError as exc:
            logger.warning("会话 %s checkpoint 保存失败: %s", key, exc)
            return False

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
