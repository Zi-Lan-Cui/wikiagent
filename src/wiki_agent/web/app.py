"""FastAPI adapter——本地单用户服务；后台循环由 AppRuntime 统一装配。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from wiki_agent.application import (
    InvalidInputError,
    SessionNotFoundError,
    WikiAgentService,
)
from wiki_agent.application.issue_actions import IssueActionExecutor
from wiki_agent.application.job_results import JobResult
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.compiler.workflows.retry import SourceUnavailableError
from wiki_agent.issues import (
    InvalidIssueTransitionError,
    IssueAlreadyClaimedError,
    IssueKind,
    IssueNotFoundError,
    IssueStatus,
)
from wiki_agent.jobs import SyncInProgress
from wiki_agent.log import setup_event_log
from wiki_agent.wiki import WikiPageNotFound


class CreateSessionRequest(BaseModel):
    title: str = Field(default="未命名", max_length=200)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class IssueActionRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


def create_app(
    *,
    project_root: Path | None = None,
    runtime: AppRuntime | None = None,
) -> FastAPI:
    """Create the local Web application.

    ``runtime`` is injectable for tests.  Production callers normally pass
    ``project_root`` and let the factory construct one process-wide runtime.
    """
    app_runtime = runtime or AppRuntime.from_project_root(project_root or Path.cwd())
    service = WikiAgentService(app_runtime)
    issue_actions = IssueActionExecutor(app_runtime)
    issue_actions.reconcile_retry_sources()
    # worker/维护循环由 AppRuntime 统一装配与生命周期管理；
    # web 只补 issue_action handler（executor 在 create_app 内构造）
    job_service = app_runtime.job_service
    job_worker = app_runtime.job_worker

    async def handle_issue_job(job, progress):
        # 业务拒绝（来源丢失/状态 CAS 不满足/动作非法/未实现）是终态——
        # 返回无联动语义的 failed 结果，不落 run_failure 账。
        try:
            issue_actions.execute(job.resource, job.mode, job.payload, progress=progress)
        except (
            SourceUnavailableError,
            IssueAlreadyClaimedError,
            InvalidIssueTransitionError,
            ValueError,
        ) as exc:
            return JobResult(status="failed", detail={"error": str(exc)[:500]})
        return JobResult(status="succeeded")

    if not job_worker.is_registered("issue_action"):
        job_worker.register("issue_action", handle_issue_job)

    def _in_flight_issue_ids() -> set[str]:
        # "在途"= 该 issue 有挂账的 queued/running job——一条 SQL，不扫内存
        return set(job_service.store.open_issue_ids_with_in_flight_job())

    def submit_issue_job(
        issue_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        job = job_service.submit_issue_action(issue_id, action, payload)
        return _job_task(job)

    def submit_retry_job(issue_id: str) -> dict[str, Any]:
        # retry 直投 compile job（三入口同一提交点）；双击被提交点收敛
        return _job_task(job_service.submit_issue_retry(issue_id))

    def _job_task(job) -> dict[str, Any]:
        item = asdict(job)
        issue_id = job.issue_id  # 挂账关系统一走 issue_id 列
        item["issue_id"] = issue_id
        item["action"] = job.mode
        item["current_stage"] = job.stage or ("等待执行" if job.status == "queued" else "")
        item["stage_code"] = job.stage
        item["stage_index"] = 0
        item["stage_total"] = 0
        try:
            issue = service.get_issue(issue_id) if issue_id else None
        except LookupError:
            issue = None
        if issue is not None:
            item["title"] = issue.title
            item["resource"] = str(
                issue.resource.get("path") or issue.resource.get("label") or job.resource
            )
        else:
            item["title"] = f"{job.kind} {Path(job.resource).name}"
            item["resource"] = job.resource
        if item["status"] == "succeeded":
            item["status"] = "completed"
        if job.status == "succeeded" and issue is not None:
            item["result"] = asdict(issue)
        return item

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        setup_event_log(app_runtime.workspace / "logs" / "web-events.jsonl")
        try:
            # start() 拉起 job_worker 与维护循环，close() 统一收尾
            async with app_runtime:
                yield
        finally:
            setup_event_log(None)

    app = FastAPI(title="wiki-agent", version="0.1.0", lifespan=lifespan)
    app.state.runtime = app_runtime
    app.state.service = service
    app.state.job_service = job_service
    app.state.job_worker = job_worker

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return [asdict(session) for session in service.list_sessions()]

    @app.get("/api/wiki/files")
    async def list_wiki_files() -> list[dict[str, Any]]:
        return [asdict(file) for file in service.list_wiki_files()]

    @app.get("/api/issues")
    async def list_issues(
        status: str = "open,blocked",
        kind: str = "",
        limit: int = 200,
        offset: int = 0,
        include_active_tasks: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            statuses = {IssueStatus(value) for value in status.split(",") if value}
            kinds = {IssueKind(value) for value in kind.split(",") if value} or None
            cards = service.list_issues(
                statuses=statuses or None,
                kinds=kinds,
                limit=limit,
                offset=offset,
            )
            if not include_active_tasks:
                active_ids = _in_flight_issue_ids()
                cards = [card for card in cards if card.id not in active_ids]
            return [asdict(card) for card in cards]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/issues/summary")
    async def issue_summary() -> dict[str, int]:
        active_task_issues = _in_flight_issue_ids()
        active_issues = service.list_issues(
            statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
            limit=1000,
        )
        return {
            "active": sum(card.id not in active_task_issues for card in active_issues),
            "retryable": len(
                issue_actions.retry_batch_candidates(exclude_issue_ids=active_task_issues)
            ),
        }

    @app.post("/api/issues/actions/retry-eligible", status_code=202)
    async def retry_eligible_issues() -> dict[str, Any]:
        try:
            issue_ids = issue_actions.prepare_retry_batch(exclude_issue_ids=_in_flight_issue_ids())
            tasks = [submit_retry_job(issue_id) for issue_id in issue_ids]
            return {"count": len(tasks), "tasks": tasks}
        except (IssueAlreadyClaimedError, SourceUnavailableError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/sync", status_code=202)
    async def trigger_sync() -> dict[str, Any]:
        """快照同步：拍 materials 现状入队一批；上一次批次未跑完则 409。"""
        try:
            jobs = job_service.submit_sync(app_runtime.materials_dir)
        except SyncInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"count": len(jobs), "tasks": [_job_task(job) for job in jobs]}

    @app.get("/api/sync/status")
    async def sync_status() -> dict[str, int]:
        return job_service.sync_status(app_runtime.materials_dir)

    @app.get("/api/issues/{issue_id}")
    async def get_issue(issue_id: str) -> dict[str, Any]:
        try:
            return asdict(service.get_issue(issue_id))
        except IssueNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"问题不存在: {issue_id}") from exc

    @app.get("/api/issues/{issue_id}/resource")
    async def get_issue_resource(issue_id: str) -> dict[str, Any]:
        try:
            return asdict(service.get_issue_resource(issue_id))
        except IssueNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"问题不存在: {issue_id}") from exc
        except WikiPageNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/issues/{issue_id}/actions/{action}", response_model=None)
    async def execute_issue_action(issue_id: str, action: str, request: IssueActionRequest) -> Any:
        try:
            if action == "retry":
                issue_actions.validate(issue_id, action)
                return JSONResponse(status_code=202, content=submit_retry_job(issue_id))
            if action == "rescan":
                issue_actions.validate(issue_id, action)
                task = submit_issue_job(issue_id, action, request.payload)
                return JSONResponse(status_code=202, content=task)
            return asdict(issue_actions.execute(issue_id, action, request.payload))
        except IssueNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"问题不存在: {issue_id}") from exc
        except IssueAlreadyClaimedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SourceUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/issue-tasks/{task_id}")
    async def get_issue_task(task_id: str) -> dict[str, Any]:
        try:
            return _job_task(job_service.store.get(task_id))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}") from exc

    @app.get("/api/issue-tasks")
    async def list_issue_tasks(limit: int = 100) -> list[dict[str, Any]]:
        try:
            jobs = [job for job in job_service.list(limit=limit) if job.issue_id]
            return [_job_task(job) for job in jobs]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs")
    async def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
        try:
            return [_job_task(job) for job in job_service.list(limit=limit)]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/queue")
    async def list_failure_queue_compatibility() -> list[dict[str, Any]]:
        """Compatibility alias while the old queue UI is being retired."""
        cards = service.list_issues(statuses={IssueStatus.OPEN, IssueStatus.BLOCKED})
        return [asdict(card) for card in cards]

    @app.get("/api/wiki/pages/{page_path:path}")
    async def get_wiki_page(page_path: str) -> dict[str, Any]:
        try:
            return asdict(service.get_wiki_page(page_path))
        except WikiPageNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/wiki/sources/{source_path:path}")
    async def get_wiki_source(source_path: str) -> dict[str, Any]:
        try:
            return asdict(service.get_wiki_source(source_path))
        except WikiPageNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/wiki/search")
    async def search_wiki_pages(q: str, limit: int = 30) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="limit 必须在 1 到 100 之间")
        return [asdict(page) for page in service.search_wiki_pages(q, limit=limit)]

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        try:
            return asdict(service.create_session(title=request.title))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            return asdict(service.get_session(session_id))
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/messages")
    async def get_session_messages(session_id: str) -> list[dict[str, str]]:
        try:
            return [asdict(message) for message in service.get_session_messages(session_id)]
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: MessageRequest) -> dict[str, Any]:
        try:
            result = await service.send_message(session_id, request.text)
            return asdict(result)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/messages/stream")
    async def stream_message(session_id: str, request: MessageRequest) -> StreamingResponse:
        try:
            service.get_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def events() -> AsyncIterator[str]:
            try:
                async for event in service.stream_message(session_id, request.text):
                    payload = json.dumps(asdict(event), ensure_ascii=False)
                    yield f"event: {event.type}\ndata: {payload}\n\n"
            except (SessionNotFoundError, InvalidInputError) as exc:
                payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    frontend_dir = (project_root or Path.cwd()) / "frontend"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


app = create_app()
