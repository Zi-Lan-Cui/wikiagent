"""Application use cases shared by CLI, Web and future adapters."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from wiki_agent.conversation import Session
from wiki_agent.events import AgentEvent
from wiki_agent.issues import IssueCard, IssueKind, IssueStatus
from wiki_agent.wiki import (
    WikiPage,
    read_authorized_source,
    read_page,
    read_source,
    search_pages,
)

if TYPE_CHECKING:
    # 装配根仅作类型注解（from __future__ import annotations）——真导入会把
    # 整个执行栈（watch/agent/llm/…）拖进本模块的 import 环。
    from wiki_agent.application.runtime import AppRuntime


class ServiceError(Exception):
    """Base error raised at the application boundary."""


class SessionNotFoundError(ServiceError):
    """Raised when an operation refers to a non-existent session."""


class InvalidInputError(ServiceError):
    """Raised when a caller supplies invalid user input."""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Stable session representation exposed to adapters."""

    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    message_count: int


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """User-visible message returned by the history API."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class MessageResult:
    """Result of one completed Agent turn."""

    run_id: str
    session: SessionInfo
    assistant_text: str


@dataclass(frozen=True, slots=True)
class WikiFileInfo:
    """Read-only metadata for a generated Wiki file."""

    path: str
    size: int
    updated_at: str


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DEFAULT_SESSION_TITLE = "未命名"
_SESSION_TITLE_MAX_LENGTH = 40


class WikiAgentService:
    """Coordinate user-facing operations on an :class:`AppRuntime`.

    This class deliberately contains no FastAPI, Rich or HTTP concepts.  It
    is the shared application boundary used by CLI and future Web adapters.
    """

    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime

    @property
    def session_manager(self):
        """Return the runtime's shared session manager."""
        return self.runtime.agent.session_manager

    def create_session(self, *, title: str = "未命名") -> SessionInfo:
        """Create and persist an empty session."""
        session_id = f"session_{uuid4().hex}"
        session = Session(key=session_id)
        session.session_title = title.strip() or _DEFAULT_SESSION_TITLE
        if not self.session_manager.save_checkpoint(session):
            raise ServiceError(f"无法保存会话: {session_id}")
        return self._to_info(session)

    def list_sessions(self) -> list[SessionInfo]:
        """List persisted sessions, newest first."""
        return [
            self.get_session(session_id) for session_id in self.session_manager.list_session_keys()
        ]

    def list_wiki_files(self) -> list[WikiFileInfo]:
        """List generated Markdown files below the configured Wiki root."""
        if not self.runtime.wiki_dir.is_dir():
            return []
        files: list[WikiFileInfo] = []
        for path in sorted(self.runtime.wiki_dir.rglob("*.md")):
            if not path.is_file() or any(part.startswith(".") for part in path.parts):
                continue
            stat = path.stat()
            files.append(
                WikiFileInfo(
                    path=path.relative_to(self.runtime.wiki_dir).as_posix(),
                    size=stat.st_size,
                    updated_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                )
            )
        return files

    def list_issues(
        self,
        *,
        statuses: set[IssueStatus] | None = None,
        kinds: set[IssueKind] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[IssueCard]:
        """Return sanitized issue cards shared by all adapters."""
        return self.runtime.issue_service.list(
            statuses=statuses,
            kinds=kinds,
            limit=limit,
            offset=offset,
        )

    def get_issue(self, issue_id: str) -> IssueCard:
        return self.runtime.issue_service.get(issue_id)

    def get_issue_resource(self, issue_id: str) -> WikiPage:
        """Read the resource bound to an issue without exposing its private path."""
        issue = self.runtime.issue_store.require(issue_id)
        public_path = str(issue.resource.get("path") or issue.resource.get("label") or "")
        source_path = issue.context.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            target = Path(source_path)
            if not target.is_absolute():
                target = self.runtime.config.paths.project_root / target
            try:
                relative = target.resolve().relative_to(self.runtime.wiki_dir.resolve())
            except ValueError:
                return read_authorized_source(target, label=public_path)
            return read_page(self.runtime.wiki_dir, relative.as_posix())
        if issue.resource.get("type") == "wiki_page":
            return read_page(self.runtime.wiki_dir, public_path)
        return read_source(self.runtime.source_records_dir, public_path)

    def count_active_issues(self) -> int:
        return self.runtime.issue_service.count(statuses={IssueStatus.OPEN, IssueStatus.BLOCKED})

    def get_wiki_page(self, path: str) -> WikiPage:
        """Read one public Wiki page using the shared safe resolver."""
        return read_page(self.runtime.wiki_dir, path)

    def get_wiki_source(self, path: str) -> WikiPage:
        """Read one source through the Web-only read boundary."""
        return read_source(self.runtime.source_records_dir, path)

    def search_wiki_pages(self, query: str, *, limit: int = 30) -> list[WikiPage]:
        """Search public Wiki page paths and contents."""
        return search_pages(self.runtime.wiki_dir, query, limit=limit)

    def get_session(self, session_id: str) -> SessionInfo:
        """Load one persisted session or raise a boundary error."""
        self._validate_session_id(session_id)
        if not (self.session_manager.sessions_dir / f"{session_id}.jsonl").is_file():
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        return self._to_info(self.session_manager.get_or_create(session_id))

    def get_session_messages(self, session_id: str) -> list[SessionMessage]:
        """Return visible chat history, excluding internal tool/system turns."""
        self._validate_session_id(session_id)
        session = self._get_loaded_session(session_id)
        return [
            SessionMessage(role=message.role, content=message.content)
            for message in session.history
            if message.role == "user" or (message.role == "assistant" and message.content.strip())
        ]

    async def send_message(self, session_id: str, text: str) -> MessageResult:
        """Run one user turn and return a stable result snapshot."""
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
        """Yield lifecycle events for one Agent turn in sequence order."""
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
        """Run an Agent task while draining its per-run event queue."""
        task: asyncio.Task[None] | None = None
        async with self.runtime.event_publisher.subscribe(run_id) as queue:
            task = asyncio.create_task(
                self.runtime.agent.run(
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
        if not (self.session_manager.sessions_dir / f"{session_id}.jsonl").is_file():
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        try:
            return self.session_manager.get_or_create(session_id)
        except (OSError, ValueError) as exc:
            raise SessionNotFoundError(f"无法加载会话: {session_id}") from exc

    def _ensure_session_title(self, session: Session, query: str) -> None:
        """Set a useful title from the first query, preserving custom titles."""
        if session.session_title.strip() != _DEFAULT_SESSION_TITLE:
            return
        if any(message.role == "user" for message in session.history):
            return
        title = self.title_from_query(query)
        if title:
            session.session_title = title
            if not self.session_manager.save_checkpoint(session):
                raise ServiceError(f"无法保存会话标题: {session.key}")

    @staticmethod
    def title_from_query(query: str, max_length: int = _SESSION_TITLE_MAX_LENGTH) -> str:
        """Return a compact, deterministic title derived from user text."""
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
        # Tool messages are internal execution traces, not user-visible chat
        # turns.  Counting them made the UI appear to gain messages after a
        # refresh whenever a turn used tools.
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
