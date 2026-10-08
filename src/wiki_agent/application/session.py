"""会话用例：会话生命周期、历史读取与一次问答回合的驱动。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from wiki_agent.conversation import Session
from wiki_agent.events import AgentEvent
from wiki_agent.log import get_logger

logger = get_logger("SESSION")

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
    # 思考过程分段（文字段 + 工具动作段），按回合顺序聚合
    thinking: list[dict] = field(default_factory=list)


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
        # 后台标题生成任务持引用，防止未 await 前被 GC 回收
        self._title_tasks: set[asyncio.Task] = set()

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
        # 一次提问的 ReAct 循环会产生多条 assistant 消息（工具回合的
        # content 常为空）；thinking 分段按回合顺序聚合，随下一个可见回答返回
        out: list[SessionMessage] = []
        pending: list[dict] = []
        for message in session.history:
            if message.role == "user":
                out.append(SessionMessage(role="user", content=message.content))
                pending = []
            elif message.role == "assistant":
                pending.extend(seg.model_dump(exclude_defaults=True) for seg in message.thinking)
                if message.content.strip():
                    out.append(
                        SessionMessage(role="assistant", content=message.content, thinking=pending)
                    )
                    pending = []
        return out

    async def send_message(self, session_id: str, text: str) -> MessageResult:
        """跑完一个用户回合，返回稳定的结果快照。"""
        self._validate_session_id(session_id)
        text = text.strip()
        if not text:
            raise InvalidInputError("消息内容不能为空")
        session = self._get_loaded_session(session_id)
        run_id = f"run_{uuid4().hex}"
        async for event in self._stream_message(session_id, text, run_id):
            if event.type == "run_error":
                raise ServiceError(str(event.data.get("error") or "Agent 运行失败"))
        # 同步接口等标题落定后再返回：CLI/脚本调用完即退出进程，
        # 不能把后台标题任务留在退出路径上
        if self._title_tasks:
            await asyncio.gather(*list(self._title_tasks), return_exceptions=True)
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
        run_id = f"run_{uuid4().hex}"
        async for event in self._stream_message(session_id, text, run_id):
            yield event

    async def _stream_message(
        self,
        session_id: str,
        text: str,
        run_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """执行 agent 任务，同时消费该回合的事件队列。

        首轮正常结束后在后台发起标题生成，不阻塞 run_finished 事件；
        标题全程只落一次盘（LLM 失败时改用截断标题），回合进行中新会话
        保持"未命名"，避免先写占位标题、再改正式标题的两次更新。
        """
        session = self._get_loaded_session(session_id)
        is_first_turn = session.session_title.strip() == _DEFAULT_SESSION_TITLE
        stop_reason = ""

        def _capture(event: AgentEvent) -> None:
            nonlocal stop_reason
            if event.type == "run_finished":
                stop_reason = str(event.data.get("stop_reason") or "")

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
                        _capture(event)
                        yield event
                        if event.type == "run_finished":
                            await task
                            break
                    else:
                        event_task.cancel()
                        await asyncio.gather(event_task, return_exceptions=True)
                        await task
                        while not queue.empty():
                            queued = queue.get_nowait()
                            _capture(queued)
                            yield queued
                        break
            except BaseException:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        # 只在回合正常结束时发起标题生成；取消或出错的回合不发起调用
        if is_first_turn and stop_reason not in ("cancelled", "error"):
            self._schedule_session_title(session, text)

    def _schedule_session_title(
        self, session: Session, question: str
    ) -> asyncio.Task[None]:
        """后台一次性生成标题：LLM 成功用其结果，失败改用截断标题。

        标题只在这一次落盘；期间用户手动改过名（已非"未命名"）则放弃。
        返回创建的任务以便同步路径等待（CLI 进程会立即退出）。
        """

        async def _work() -> None:
            answer = self._last_assistant_text(session)
            title = ""
            if answer.strip():
                try:
                    raw = await self._agent.generate_session_title(question, answer)
                except Exception as exc:
                    logger.info("会话标题生成失败，退回截断标题: %s", type(exc).__name__)
                else:
                    title = " ".join(raw.split())[:_SESSION_TITLE_MAX_LENGTH]
            title = title or self.title_from_query(question)
            if not title or session.session_title.strip() != _DEFAULT_SESSION_TITLE:
                return
            session.session_title = title
            await self._session_manager.asave(session)

        task = asyncio.create_task(_work(), name=f"wiki-title:{session.key}")
        self._title_tasks.add(task)
        task.add_done_callback(self._title_tasks.discard)
        return task

    def _get_loaded_session(self, session_id: str) -> Session:
        if not (self._session_manager.sessions_dir / f"{session_id}.jsonl").is_file():
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        try:
            return self._session_manager.get_or_create(session_id)
        except (OSError, ValueError) as exc:
            raise SessionNotFoundError(f"无法加载会话: {session_id}") from exc

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
        # 只统计用户可见轮次；计入工具消息会让界面显示不存在的聊天轮次。
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
