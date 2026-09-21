"""Problem-center HTTP adapter integration tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import httpx

from wiki_agent.application.job_service import JobService
from wiki_agent.application.job_worker import JobWorker
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.issues import IssueDraft, IssueKind, IssueService, IssueStatus, IssueStore
from wiki_agent.log import emit_event
from wiki_agent.sync.state import SyncState
from wiki_agent.web.app import create_app


class _Runtime:
    def __init__(self, root: Path):
        self.workspace = root / "workspace"
        self.wiki_dir = root / "wiki"
        self.wiki_dir.mkdir()
        self.materials_dir = root / "materials"
        self.materials_dir.mkdir()
        self.issue_store = IssueStore(self.workspace)
        self.issue_service = IssueService(self.issue_store)
        # 统一执行模型：web 装配从 runtime 拿 job_service/job_worker
        self.job_service = JobService(
            self.workspace,
            wiki_dir=self.wiki_dir,
            sync_state=SyncState(self.workspace / "watch" / "state.json"),
        )
        self.job_worker = JobWorker(self.job_service)
        self._worker_task = None

    async def __aenter__(self):
        self._worker_task = asyncio.create_task(self.job_worker.run())
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.job_worker.stop()
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        return None


def test_web_lifespan_writes_structured_event_log(tmp_path: Path):
    runtime = _Runtime(tmp_path)
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def run():
        async with app.router.lifespan_context(app):
            emit_event("web_test_event", detail="recorded")

    asyncio.run(run())

    event_log = runtime.workspace / "logs" / "web-events.jsonl"
    assert event_log.is_file()
    assert '"event": "web_test_event"' in event_log.read_text(encoding="utf-8")


def test_issue_api_lists_decides_and_reports_summary(tmp_path: Path):
    runtime = _Runtime(tmp_path)
    source = tmp_path / "private-notes" / "example.md"
    source.parent.mkdir()
    source.write_text("# Original source\n\nprivate path stays server-side", encoding="utf-8")
    issue = runtime.issue_service.report(
        IssueDraft(
            kind=IssueKind.CONTENT_CORRECTION,
            title="页面说法纠错",
            summary="示例页与来源不符",
            resource={"type": "input_file", "path": "example.md"},
            context={"source_path": str(source)},
        )
    )
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/issues/summary")).json() == {
                "active": 1,
                "retryable": 0,
            }
            listed = (await client.get("/api/issues")).json()
            assert [item["id"] for item in listed] == [issue.id]
            assert str(tmp_path) not in str(listed)
            resource = (await client.get(f"/api/issues/{issue.id}/resource")).json()
            assert resource["path"] == "sources/example.md"
            assert "Original source" in resource["content"]
            decided = await client.post(
                f"/api/issues/{issue.id}/actions/keep_uncertain",
                json={"payload": {}},
            )
            assert decided.status_code == 200
            assert decided.json()["status"] == "blocked"

    asyncio.run(run())


def test_long_issue_action_returns_pollable_task(tmp_path: Path):
    runtime = _Runtime(tmp_path)
    issue = runtime.issue_service.report(
        IssueDraft(
            kind=IssueKind.QUALITY_ISSUE,
            title="页面质量告警",
            summary="需要重新扫描",
            resource={"type": "wiki_page", "path": "concepts/example.md"},
        )
    )
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def run():
        # worker 循环由 lifespan 拉起（生产 uvicorn 必经），ASGITransport 需手动进入
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post(
                f"/api/issues/{issue.id}/actions/rescan",
                json={"payload": {}},
            )
            assert response.status_code == 202
            task_id = response.json()["id"]
            queued = (await client.get("/api/issue-tasks")).json()
            assert queued[0]["id"] == task_id
            assert queued[0]["resource"] == "concepts/example.md"
            task = {}
            for _ in range(20):
                task = (await client.get(f"/api/issue-tasks/{task_id}")).json()
                if task["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert task["status"] == "completed"
            assert task["result"]["status"] == "resolved"

    asyncio.run(run())


def test_missing_retry_source_is_blocked_before_task_creation(tmp_path: Path):
    runtime = _Runtime(tmp_path)
    issue = runtime.issue_service.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="missing.md 处理失败",
            summary="原始来源已丢失",
            origin={"mode": "compile"},
            resource={"type": "input_file", "path": "missing.md"},
            context={"source_path": str(tmp_path / "missing.md")},
        )
    )
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/issues/{issue.id}/actions/retry",
                json={"payload": {}},
            )
            assert response.status_code == 409
            assert "原始来源已不存在" in response.json()["detail"]
            assert (await client.get("/api/issue-tasks")).json() == []
            card = (await client.get(f"/api/issues/{issue.id}")).json()
            assert card["status"] == "blocked"
            retry = next(action for action in card["available_actions"] if action["id"] == "retry")
            assert "原始来源已不存在" in retry["disabled_reason"]

    asyncio.run(run())


def test_web_retry_returns_compile_task_and_converges(tmp_path: Path):
    """retry 直投 compile job：202 携带挂账 task；双击收敛为同一 task_id。"""
    runtime = _Runtime(tmp_path)
    source = tmp_path / "available-retry.md"
    source.write_text("# 可重试", encoding="utf-8")
    issue = runtime.issue_service.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="available-retry.md 处理失败",
            summary="临时失败",
            origin={"mode": "compile"},
            resource={"type": "input_file", "path": source.name},
            context={"source_path": str(source)},
        )
    )
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def run():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            first = await client.post(f"/api/issues/{issue.id}/actions/retry", json={"payload": {}})
            assert first.status_code == 202
            task = first.json()
            assert task["kind"] == "compile" and task["action"] == "issue_retry"
            assert task["issue_id"] == issue.id
            # 双击：提交点收敛返回同一 task——不再 409
            second = await client.post(
                f"/api/issues/{issue.id}/actions/retry", json={"payload": {}}
            )
            assert second.status_code == 202
            assert second.json()["id"] == task["id"]
            listed = (await client.get("/api/issue-tasks")).json()
            assert [t["id"] for t in listed] == [task["id"]]
            # issue 保持 open——在途由挂账 job 表达
            card = (await client.get(f"/api/issues/{issue.id}")).json()
            assert card["status"] == "open"

    asyncio.run(run())


def test_bulk_retry_enqueues_available_manual_sources(tmp_path: Path):
    runtime = _Runtime(tmp_path)
    source = tmp_path / "notes" / "available.md"
    source.parent.mkdir()
    source.write_text("# available", encoding="utf-8")
    expired = (datetime.now() - timedelta(hours=1)).isoformat()
    runtime.issue_service.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            status=IssueStatus.BLOCKED,
            title="available.md 处理失败",
            summary="临时失败",
            origin={"mode": "compile"},
            resource={"type": "input_file", "path": source.name},
            retry={"policy": "manual", "expires_at": expired},
            context={"source_path": str(source)},
        )
    )
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/issues/actions/retry-eligible")
            assert response.status_code == 202
            assert response.json()["count"] == 1
            assert response.json()["tasks"][0]["status"] == "queued"

    asyncio.run(run())


def test_sync_endpoint_snapshots_and_mutex(tmp_path: Path):
    """POST /api/sync 快照入队 202；同批未跑完再点 409；status 如实报数。"""
    runtime = _Runtime(tmp_path)
    (runtime.materials_dir / "a.md").write_text("同步内容" * 10, encoding="utf-8")
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def run():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            status = (await client.get("/api/sync/status")).json()
            assert status == {"dirty": 1, "removed": 0, "in_flight": 0}

            first = await client.post("/api/sync")
            assert first.status_code == 202
            body = first.json()
            assert body["count"] == 1
            assert body["tasks"][0]["kind"] == "compile" and body["tasks"][0]["action"] == "sync"

            # worker 只注册 issue_action（fixture 语义），compile 行保持 queued——
            # 快照互斥闸拒绝叠放
            second = await client.post("/api/sync")
            assert second.status_code == 409
            after = (await client.get("/api/sync/status")).json()
            assert after["dirty"] == 1 and after["in_flight"] == 1

    asyncio.run(run())
