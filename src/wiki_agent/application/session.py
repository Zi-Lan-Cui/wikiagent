"""会话用例：会话生命周期、历史读取与一次问答回合的驱动。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from wiki_agent.conversation import Session
from wiki_agent.events import AgentEvent

if TYPE_CHECKING:
    from wiki_agent.agent import ReActAgent
    from wiki_agent.conversation import SessionManager
    from wiki_agent.events import EventPublisher


class ServiceError(Exception):
    """应用边界抛出的错误基类。"""


class SessionNotFoundError(ServiceError):
    """操作指向了不存在的会话。"""


class InvalidInputError(ServiceError):
    """调用方传入了非法输入。"""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """暴露给适配器的稳定会话表示。"""

    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    message_count: int


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """历史接口返回的用户可见消息。"""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class MessageResult:
    """一次完成的 Agent 回合的结果快照。"""

    run_id: str
    session: SessionInfo
    assistant_text: str


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DEFAULT_SESSION_TITLE = "未命名"
_SESSION_TITLE_MAX_LENGTH = 40


class SessionService:
    """会话用例；依赖（agent 执行、会话持久化、事件订阅）由组合根注入。"""

    def __init__(
        self,
        *,
        agent: ReActAgent,
        session_manager: SessionManager,
        event_publisher: EventPublisher,
    ) -> None:
        self._agent = agent
        self._session_manager = session_manager
        self._event_publisher = event_publisher

    def create_session(self, *, title: str = "未命名") -> SessionInfo:
        """创建并持久化一个空会话。"""
        session_id = f"session_{uuid4().hex}"
        session = Session(key=session_id)
        session.session_title = title.strip() or _DEFAULT_SESSION_TITLE
        if not self._session_manager.save_checkpoint(session):
            raise ServiceError(f"无法保存会话: {session_id}")
        return self._to_info(session)

    def list_sessions(self) -> list[SessionInfo]:
        """按更新时间倒序列出持久化会话。"""
        return [
            self.get_session(session_id)
            for session_id in self._session_manager.list_session_keys()
        ]

    def get_session(self, session_id: str) -> SessionInfo:
        """读取单个会话，不存在时抛边界错误。"""
        self._validate_session_id(session_id)
        if not (self._session_manager.sessions_dir / f"{session_id}.jsonl").is_file():
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        return self._to_info(self._session_manager.get_or_create(session_id))

    def get_session_messages(self, session_id: str) -> list[SessionMessage]:
        """返回用户可见的聊天历史，工具/系统轮次不在此列。"""
        self._validate_session_id(session_id)
        session = self._get_loaded_session(session_id)
        return [
            SessionMessage(role=message.role, content=message.content)
            for message in session.history
            if message.role == "user" or (message.role == "assistant" and message.content.strip())
        ]

    async def send_message(self, session_id: str, text: str) -> MessageResult:
        """跑完一个用户回合，返回稳定的结果快照。"""
        self._validate_session_id(session_id)
        text = text.strip()
        if not text:
            raise InvalidInputError("消息内容不能为空")
        session = self._get_loaded_session(session_id)
        self._ensure_session_title(session, text)
        run_id = f"run_{uuid4().hex}"
        async for event in self._stream_message(session_id, text, run_id):
            if event.type == "run_error":
                raise ServiceError(str(event.data.get("error") or "Agent 运行失败"))
        assistant_text = self._last_assistant_text(session)
        return MessageResult(
            run_id=run_id,
            session=self._to_info(session),
            assistant_text=assistant_text,
        )

    async def stream_message(self, session_id: str, text: str) -> AsyncIterator[AgentEvent]:
        """按顺序产出一次 Agent 回合的生命周期事件。"""
        self._validate_session_id(session_id)
        text = text.strip()
        if not text:
            raise InvalidInputError("消息内容不能为空")
        session = self._get_loaded_session(session_id)
        self._ensure_session_title(session, text)
        run_id = f"run_{uuid4().hex}"
        async for event in self._stream_message(session_id, text, run_id):
            yield event

    async def _stream_message(
        self,
        session_id: str,
        text: str,
        run_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """执行 agent 任务，同时消费该回合的事件队列。"""
        task: asyncio.Task[None] | None = None
        async with self._event_publisher.subscribe(run_id) as queue:
            task = asyncio.create_task(
                self._agent.run(
                    session_key=session_id,
                    user_input=text,
                    stream=True,
                    run_id=run_id,
                ),
                name=f"wiki-agent:{run_id}",
            )
            try:
                while True:
                    event_task = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {event_task, task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if event_task in done:
                        event = event_task.result()
                        yield event
                        if event.type == "run_finished":
                            await task
                            break
                    else:
                        event_task.cancel()
                        await asyncio.gather(event_task, return_exceptions=True)
                        await task
                        while not queue.empty():
                            yield queue.get_nowait()
                        break
            except BaseException:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise

    def _get_loaded_session(self, session_id: str) -> Session:
        if not (self._session_manager.sessions_dir / f"{session_id}.jsonl").is_file():
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        try:
            return self._session_manager.get_or_create(session_id)
        except (OSError, ValueError) as exc:
            raise SessionNotFoundError(f"无法加载会话: {session_id}") from exc

    def _ensure_session_title(self, session: Session, query: str) -> None:
        """首条提问后生成实用标题，自定义标题保持不变。"""
        if session.session_title.strip() != _DEFAULT_SESSION_TITLE:
            return
        if any(message.role == "user" for message in session.history):
            return
        title = self.title_from_query(query)
        if title:
            session.session_title = title
            if not self._session_manager.save_checkpoint(session):
                raise ServiceError(f"无法保存会话标题: {session.key}")

    @staticmethod
    def title_from_query(query: str, max_length: int = _SESSION_TITLE_MAX_LENGTH) -> str:
        """从用户文本生成紧凑、确定的标题。"""
        normalized = " ".join(query.split())
        if not normalized:
            return _DEFAULT_SESSION_TITLE
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max(1, max_length - 1)].rstrip() + "…"

    @staticmethod
    def _last_assistant_text(session: Session) -> str:
        for message in reversed(session.history):
            if message.role == "assistant":
                content = message.content
                return content if isinstance(content, str) else str(content)
        return ""

    @staticmethod
    def _to_info(session: Session) -> SessionInfo:
        # 工具消息是内部执行记录，不是用户可见的聊天轮次。把它们计入
        # 会让界面在刷新后凭空多出消息。
        visible_message_count = sum(
            bool(
                message.role == "user" or (message.role == "assistant" and message.content.strip())
            )
            for message in session.history
        )
        return SessionInfo(
            id=session.key,
            title=session.session_title,
            status=session.status,
            created_at=session.created_at,
            updated_at=session.updated_at,
            message_count=visible_message_count,
        )

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise InvalidInputError("session_id 只能包含字母、数字、下划线和连字符")
