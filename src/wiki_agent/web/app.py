"""Minimal FastAPI adapter for local single-user testing."""

from __future__ import annotations

import asyncio
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
from wiki_agent.application.job_service import JobService
from wiki_agent.application.job_worker import JobWorker
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.compiler.workflows.retry import SourceUnavailableError
from wiki_agent.issues import (
    IssueAlreadyClaimedError,
    IssueKind,
    IssueNotFoundError,
    IssueStatus,
)
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
    job_service = getattr(app_runtime, "job_service", JobService(app_runtime.workspace))
    job_worker = JobWorker(job_service)

    async def handle_issue_job(job, progress):
        await issue_actions.execute(
            job.resource,
            job.mode,
            job.payload,
            progress=progress,
        )

    job_worker.register("issue_action", handle_issue_job)
    worker_task = None

    def _ensure_worker() -> None:
        nonlocal worker_task
        if worker_task is None or worker_task.done():
            worker_task = asyncio.create_task(job_worker.run(), name="wiki-agent:job-worker")

    def _active_issue_ids() -> set[str]:
        return {
            job.resource
            for job in job_service.list(limit=1000)
            if job.kind == "issue_action" and job.status in {"queued", "running"}
        }

    def submit_issue_job(
        issue_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ensure_worker()
        job = job_service.submit_issue_action(issue_id, action, payload)
        return asdict(job)

    def _job_task(job) -> dict[str, Any]:
        item = asdict(job)
        issue = service.get_issue(job.resource)
        resource = str(issue.resource.get("path") or issue.resource.get("label") or job.resource)
        item.update(
            {
                "issue_id": job.resource,
                "action": job.mode,
                "title": issue.title,
                "resource": resource,
                "current_stage": job.stage or ("等待执行" if job.status == "queued" else ""),
                "stage_code": job.stage,
                "stage_index": 0,
                "stage_total": 0,
            }
        )
        if item["status"] == "succeeded":
            item["status"] = "completed"
        if job.status == "succeeded":
            item["result"] = asdict(service.get_issue(job.resource))
        return item

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        setup_event_log(app_runtime.workspace / "logs" / "web-events.jsonl")
        try:
            async with app_runtime:
                _ensure_worker()
                try:
                    yield
                finally:
                    job_worker.stop()
                    if worker_task is not None:
                        worker_task.cancel()
                        await asyncio.gather(worker_task, return_exceptions=True)
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
        status: str = "open,blocked,processing",
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
                active_ids = _active_issue_ids()
                cards = [card for card in cards if card.id not in active_ids]
            return [asdict(card) for card in cards]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/issues/summary")
    async def issue_summary() -> dict[str, int]:
        active_task_issues = _active_issue_ids()
        active_issues = service.list_issues(
            statuses={IssueStatus.OPEN, IssueStatus.BLOCKED, IssueStatus.PROCESSING},
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
            issue_ids = issue_actions.prepare_retry_batch(exclude_issue_ids=_active_issue_ids())
            tasks = [submit_issue_job(issue_id, "retry") for issue_id in issue_ids]
            return {"count": len(tasks), "tasks": tasks}
        except (IssueAlreadyClaimedError, SourceUnavailableError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            if action in {"retry", "rescan"}:
                issue_actions.validate(issue_id, action)
                task = submit_issue_job(issue_id, action, request.payload)
                return JSONResponse(status_code=202, content=task)
            return asdict(await issue_actions.execute(issue_id, action, request.payload))
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
            jobs = [job for job in job_service.list(limit=limit) if job.kind == "issue_action"]
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
