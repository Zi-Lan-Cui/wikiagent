"""In-process background tasks for long issue actions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Protocol
from uuid import uuid4

from wiki_agent.issues.models import IssueCard, JsonObject
from wiki_agent.issues.store import IssueStore

_TASK_STAGES: tuple[tuple[str, str], ...] = (
    ("prepare", "准备版本事务"),
    ("load", "加载来源"),
    ("convert", "转换文件"),
    ("extract", "抽取内容"),
    ("search", "检索候选页面"),
    ("analyze", "分析页面关系"),
    ("plan", "生成整合计划"),
    ("execute", "生成 Wiki 页面"),
    ("scan", "扫描 Wiki 质量"),
    ("commit", "提交 Wiki 变更"),
)
_TASK_STAGE_INDEX: dict[str, int] = {
    code: index for index, (code, _) in enumerate(_TASK_STAGES, start=1)
}
_TASK_STAGE_LABEL: dict[str, str] = dict(_TASK_STAGES)


class IssueActionRunner(Protocol):
    store: IssueStore

    async def execute(
        self,
        issue_id: str,
        action: str,
        payload: JsonObject | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> IssueCard: ...


@dataclass(frozen=True, slots=True)
class IssueTaskInfo:
    id: str
    issue_id: str
    action: str
    status: str
    title: str
    resource: str
    source_stage: str
    current_stage: str
    stage_code: str
    stage_index: int
    stage_total: int
    created_at: str
    updated_at: str
    result: IssueCard | None = None
    error: str = ""


class IssueTaskManager:
    """Run long actions through one in-process consumer.

    Wiki mutations are serialized because they share one versioning lock.
    Tasks that have not reached the consumer remain visibly queued.
    """

    def __init__(self, executor: IssueActionRunner):
        self._executor = executor
        self._info: dict[str, IssueTaskInfo] = {}
        self._queue: asyncio.Queue[tuple[str, str, str, JsonObject | None]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    def start(self, issue_id: str, action: str, payload: JsonObject | None = None) -> IssueTaskInfo:
        existing = next(
            (
                info
                for info in self._info.values()
                if info.issue_id == issue_id
                and info.action == action
                and info.status in {"queued", "running"}
            ),
            None,
        )
        if existing is not None:
            return existing
        issue = self._executor.store.require(issue_id)
        task_id = f"issue_task_{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        raw_resource = str(issue.resource.get("path") or issue.resource.get("label") or "")
        resource = (
            PurePath(raw_resource).name if PurePath(raw_resource).is_absolute() else raw_resource
        )
        info = IssueTaskInfo(
            id=task_id,
            issue_id=issue_id,
            action=action,
            status="queued",
            title=issue.title,
            resource=resource,
            source_stage=str(issue.origin.get("stage") or ""),
            current_stage="等待执行",
            stage_code="",
            stage_index=0,
            stage_total=len(_TASK_STAGES),
            created_at=now,
            updated_at=now,
        )
        self._info[task_id] = info
        self._queue.put_nowait((task_id, issue_id, action, payload))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._consume(),
                name="wiki-agent:issue-task-consumer",
            )
        return info

    def get(self, task_id: str) -> IssueTaskInfo:
        try:
            return self._info[task_id]
        except KeyError as exc:
            raise LookupError(task_id) from exc

    def list(self, *, limit: int = 100) -> list[IssueTaskInfo]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        priority = {"running": 0, "queued": 1, "failed": 2, "cancelled": 3, "completed": 4}
        return sorted(
            self._info.values(),
            key=lambda item: (priority.get(item.status, 9), item.created_at),
        )[:limit]

    def active_issue_ids(self) -> set[str]:
        """返回已入队或正在执行的问题，用于生成互斥的工作台投影。"""
        return {
            info.issue_id for info in self._info.values() if info.status in {"queued", "running"}
        }

    def _update(self, task_id: str, **changes: object) -> None:
        self._info[task_id] = replace(
            self._info[task_id],
            updated_at=datetime.now(UTC).isoformat(),
            **changes,
        )

    def _progress(self, task_id: str, stage: str) -> None:
        code = stage.strip().lower()
        if code == "rollback":
            self._update(
                task_id,
                current_stage="回撤 Wiki 变更",
                stage_code=code,
            )
            return
        index = _TASK_STAGE_INDEX.get(code, 0)
        self._update(
            task_id,
            current_stage=_TASK_STAGE_LABEL.get(code, stage),
            stage_code=code if index else "",
            stage_index=index,
        )

    async def _consume(self) -> None:
        while True:
            task_id, issue_id, action, payload = await self._queue.get()
            try:
                await self._run(task_id, issue_id, action, payload)
            finally:
                self._queue.task_done()

    async def _run(
        self,
        task_id: str,
        issue_id: str,
        action: str,
        payload: JsonObject | None,
    ) -> None:
        self._update(task_id, status="running", current_stage="准备执行")
        try:
            result = await self._executor.execute(
                issue_id,
                action,
                payload,
                progress=lambda stage: self._progress(task_id, stage),
            )
        except asyncio.CancelledError:
            self._update(
                task_id,
                status="cancelled",
                current_stage="已取消",
                error="process_stopped",
            )
            raise
        except Exception as exc:
            self._update(
                task_id,
                status="failed",
                current_stage="执行失败",
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            self._update(
                task_id,
                status="completed",
                current_stage="已完成",
                result=result,
            )

    async def close(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        for task_id, info in tuple(self._info.items()):
            if info.status == "queued":
                self._update(
                    task_id,
                    status="cancelled",
                    current_stage="服务已停止",
                    error="process_stopped",
                )
        self._executor.store.recover_interrupted_actions(reason="process_stopped")
