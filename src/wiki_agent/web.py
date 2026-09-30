"""FastAPI adapter——本地单用户服务；后台循环由 AppRuntime 统一装配。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from wiki_agent.application import InvalidInputError, ServiceError, SessionNotFoundError
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.compiler.restructure import UnitError
from wiki_agent.issues import (
    IssueActionConflict,
    IssueAlreadyClaimedError,
    IssueKind,
    IssueNotFoundError,
    IssueStatus,
)
from wiki_agent.jobs import PipelineBusy, SyncBaselineLag
from wiki_agent.jobs.card_view import task_card
from wiki_agent.jobs.retry_source import SourceUnavailableError
from wiki_agent.log import setup_event_log
from wiki_agent.wiki import WikiPageNotFound


class CreateSessionRequest(BaseModel):
    title: str = Field(default="未命名", max_length=200)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class IssueActionRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class MaintenanceSubmitRequest(BaseModel):
    units: list[dict[str, Any]] = Field(default_factory=list)


class LinkBatchRequest(BaseModel):
    slugs: list[str] | None = None


class PreviewResolveRequest(BaseModel):
    by: Literal["dismissed", "submitted"]


def create_app(
    *,
    project_root: Path | None = None,
    runtime: AppRuntime | None = None,
) -> FastAPI:
    """创建本地 Web 应用。

    Args:
        project_root: 项目根，生产入口传入，工厂据此构造进程级 runtime。
        runtime: 测试注入用；传入后不再自行构造。
    """
    # executor/handler/启动核对、worker 泵都由 AppRuntime 在装配根完成；
    # web 只做 HTTP 映射
    app_runtime = runtime or AppRuntime.from_project_root(project_root)
    # 端口都取自装配根：会话服务、wiki 读模型、issue 读服务、命令端口
    session_service = app_runtime.session
    browser = app_runtime.wiki_browser
    issue_service = app_runtime.issue_service
    issue_actions = app_runtime.issue_actions
    job_service = app_runtime.job_service

    def _in_flight_issue_ids() -> set[str]:
        return job_service.in_flight_issue_ids()

    def submit_issue_job(
        issue_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        job = job_service.submit_issue_action(issue_id, action, payload)
        return _task(job)

    def submit_retry_job(issue_id: str) -> dict[str, Any]:
        # retry 直投 compile job（三入口同一提交点）；双击被提交点收敛
        return _task(job_service.submit_issue_retry(issue_id))

    def _issue_or_none(issue_id: str):
        try:
            return issue_service.get(issue_id)
        except LookupError:
            return None

    def _task(job):
        """队列卡片投影——视图规则在 jobs.card_view。"""
        return task_card(job, _issue_or_none)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        setup_event_log(app_runtime.workspace / "logs" / "web-events.jsonl")
        try:
            # start() 拉起 job_worker 泵，close() 统一收尾
            async with app_runtime:
                yield
        finally:
            setup_event_log(None)

    app = FastAPI(title="wiki-agent", version="0.1.0", lifespan=lifespan)

    # 异常→HTTP 状态集中映射：端点只写业务，站点级 try/except 全部撤除。
    # 更具体的类型先注册（Starlette 按 MRO 找最近处理器）。
    _status_map: list[tuple[type[Exception], int]] = [
        (IssueNotFoundError, 404),
        (SessionNotFoundError, 404),
        (WikiPageNotFound, 404),
        (InvalidInputError, 400),
        (UnitError, 400),
        (IssueAlreadyClaimedError, 409),
        (IssueActionConflict, 409),
        (SourceUnavailableError, 409),
        (PipelineBusy, 409),
        (SyncBaselineLag, 409),
        (LookupError, 404),  # 任务不存在等裸键缺失
        (ValueError, 400),  # 参数非法兜底
        (ServiceError, 500),  # 存储失败等服务内错误，detail 透出
    ]

    def _make_handler(code: int):
        async def handler(_: Request, exc: Exception) -> JSONResponse:
            detail = str(exc).strip() or ("资源不存在" if code == 404 else type(exc).__name__)
            return JSONResponse(status_code=code, content={"detail": detail})
        return handler

    for _exc_type, _code in _status_map:
        app.add_exception_handler(_exc_type, _make_handler(_code))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return [asdict(session) for session in session_service.list_sessions()]

    @app.get("/api/wiki/files")
    async def list_wiki_files() -> list[dict[str, Any]]:
        return [asdict(file) for file in browser.list_wiki_files()]

    @app.get("/api/issues")
    async def list_issues(
        status: str = "open,blocked",
        kind: str = "",
        limit: int = 200,
        offset: int = 0,
        include_active_tasks: bool = False,
    ) -> list[dict[str, Any]]:
        statuses = {IssueStatus(value) for value in status.split(",") if value}
        kinds = {IssueKind(value) for value in kind.split(",") if value} or None
        cards = issue_service.list(
            statuses=statuses or None,
            kinds=kinds,
            limit=limit,
            offset=offset,
        )
        if not include_active_tasks:
            active_ids = _in_flight_issue_ids()
            cards = [card for card in cards if card.id not in active_ids]
        return [asdict(card) for card in cards]

    @app.get("/api/issues/summary")
    async def issue_summary() -> dict[str, int]:
        active_task_issues = _in_flight_issue_ids()
        active_issues = issue_service.list(
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
        issue_ids = issue_actions.retry_batch_candidates(exclude_issue_ids=_in_flight_issue_ids())
        jobs = job_service.submit_issue_retry_batch(issue_ids)
        return {"count": len(jobs), "tasks": [_task(job) for job in jobs]}

    @app.post("/api/sync", status_code=202)
    async def trigger_sync() -> dict[str, Any]:
        """快照同步：拍 materials 现状入队一批；上一批未跑完则 409。"""
        jobs = job_service.submit_sync(app_runtime.materials_dir)
        return {"count": len(jobs), "tasks": [_task(job) for job in jobs]}

    @app.get("/api/sync/status")
    async def sync_status() -> dict[str, int]:
        return job_service.sync_status(app_runtime.materials_dir)

    # 维护线：结构重组预览/提交、关联扫提交——执行走同一队列与泵

    @app.post("/api/maintenance/preview", status_code=202)
    async def maintenance_preview() -> dict[str, Any]:
        """整理结构分析入队：提议→复核→消解，结果进任务清单。

        分析要一到几分钟，不吊住请求——入队后在"进行中的任务"看阶段，
        完成点"查看建议"打开清单。花钱前闸在提交口（写任务在途、基线
        落后或已有分析在途 409），基于动盘的提议没有执行价值。
        """
        return {"task": _task(job_service.submit_maintenance_preview())}

    @app.post("/api/maintenance/preview/{job_id}/resolve")
    async def maintenance_preview_resolve(job_id: str, request: PreviewResolveRequest) -> dict[str, Any]:
        """处置一次分析结果（dismissed 否决 / submitted 已入队）：建议行撤下。"""
        return {"task": _task(job_service.resolve_maintenance_preview(job_id, by=request.by))}

    @app.post("/api/maintenance", status_code=202)
    async def maintenance_submit(request: MaintenanceSubmitRequest) -> dict[str, Any]:
        """确认后的单元清单整批入队（批尾自动跟波及面补链），整批同 batch。"""
        jobs = job_service.submit_maintenance(request.units)
        if not jobs:
            raise HTTPException(status_code=400, detail="单元清单为空")
        return {
            "count": len(jobs),
            "batch": str(jobs[0].payload.get("batch") or ""),
            "tasks": [_task(job) for job in jobs],
        }

    @app.post("/api/link", status_code=202)
    async def link_submit(request: LinkBatchRequest) -> dict[str, Any]:
        """关联扫入队：指定页（默认全库内容页），一页一 job 一提交。"""
        jobs = job_service.submit_link_batch(slugs=request.slugs)
        batch = str(jobs[0].payload.get("batch") or "") if jobs else ""
        return {"count": len(jobs), "batch": batch, "tasks": [_task(job) for job in jobs]}

    @app.get("/api/issues/{issue_id}")
    async def get_issue(issue_id: str) -> dict[str, Any]:
        return asdict(issue_service.get(issue_id))

    @app.get("/api/issues/{issue_id}/resource")
    async def get_issue_resource(issue_id: str) -> dict[str, Any]:
        return asdict(browser.get_issue_resource(issue_id))

    @app.post("/api/issues/{issue_id}/actions/{action}", response_model=None)
    async def execute_issue_action(issue_id: str, action: str, request: IssueActionRequest) -> Any:
        if action == "retry":
            # 资格判定在提交口（JobService），入口不重复一套
            return JSONResponse(status_code=202, content=submit_retry_job(issue_id))
        if action == "rescan":
            issue_actions.validate(issue_id, action)
            return JSONResponse(status_code=202, content=submit_issue_job(issue_id, action, request.payload))
        return asdict(issue_actions.execute(issue_id, action, request.payload))

    @app.get("/api/issue-tasks/{task_id}")
    async def get_issue_task(task_id: str) -> dict[str, Any]:
        return _task(job_service.get(task_id))

    @app.get("/api/issue-tasks")
    async def list_issue_tasks(limit: int = 100) -> list[dict[str, Any]]:
        jobs = [job for job in job_service.list(limit=limit) if job.issue_id]
        return [_task(job) for job in jobs]

    @app.get("/api/jobs")
    async def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
        return [_task(job) for job in job_service.list(limit=limit)]

    @app.get("/api/wiki/pages/{page_path:path}")
    async def get_wiki_page(page_path: str) -> dict[str, Any]:
        return asdict(browser.get_wiki_page(page_path))

    @app.get("/api/wiki/sources/{source_path:path}")
    async def get_wiki_source(source_path: str) -> dict[str, Any]:
        return asdict(browser.get_wiki_source(source_path))

    @app.get("/api/wiki/search")
    async def search_wiki_pages(q: str, limit: int = 30) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="limit 必须在 1 到 100 之间")
        return [asdict(page) for page in browser.search_wiki_pages(q, limit=limit)]

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        return asdict(session_service.create_session(title=request.title))

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return asdict(session_service.get_session(session_id))

    @app.get("/api/sessions/{session_id}/messages")
    async def get_session_messages(session_id: str) -> list[dict]:
        return [asdict(message) for message in session_service.get_session_messages(session_id)]

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: MessageRequest) -> dict[str, Any]:
        result = await session_service.send_message(session_id, request.text)
        return asdict(result)

    @app.post("/api/sessions/{session_id}/messages/stream")
    async def stream_message(session_id: str, request: MessageRequest) -> StreamingResponse:
        session_service.get_session(session_id)  # 404/400 由集中映射承接

        async def events() -> AsyncIterator[str]:
            try:
                async for event in session_service.stream_message(session_id, request.text):
                    payload = json.dumps(asdict(event), ensure_ascii=False)
                    yield f"event: {event.type}\ndata: {payload}\n\n"
            except (SessionNotFoundError, InvalidInputError) as exc:
                payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    # 前端目录跟着 runtime 的配置走：注入 runtime 时不再回退 cwd
    frontend_dir = app_runtime.config.paths.project_root / "frontend"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app
