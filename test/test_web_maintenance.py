"""维护线 HTTP 适配：preview 形状与闸、maintenance/link 提交与错误映射。

LLM 只在 preview 里被 fake 顶替；队列与判定全真（job_service 走
test_maintenance 同款装配，worker 不执行——只验证入队形状）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
from helpers import make_issue_store, make_job_service

from wiki_agent.agent import ReActAgent
from wiki_agent.application.issue_actions import IssueActionExecutor, register_job_handlers
from wiki_agent.application.restructure_service import MaintenanceOutcome
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.application.session import SessionService
from wiki_agent.application.wiki_browser import WikiBrowser
from wiki_agent.compiler.restructure import Unit
from wiki_agent.conversation import SessionManager
from wiki_agent.events import EventPublisher
from wiki_agent.issues import IssueService
from wiki_agent.jobs import Kind
from wiki_agent.jobs.worker import JobWorker
from wiki_agent.sync.state import SyncState
from wiki_agent.web import create_app

_FM = (
    "---\ntype: concept\ntitle: \"{title}\"\nsummary: \"一个足够长的摘要信息\"\n"
    "goal: \"说明该页要解决的问题\"\nrelated: []\nsources: []\n---\n"
)


class _Runtime:
    def __init__(self, root: Path):
        self.config = SimpleNamespace(paths=SimpleNamespace(project_root=root))
        self.workspace = root / "workspace"
        self.wiki_dir = root / "wiki"
        self.materials_dir = root / "materials"
        for d in (self.wiki_dir / "concepts", self.materials_dir):
            d.mkdir(parents=True)
        for slug, title in (("a", "甲"), ("b", "乙")):
            (self.wiki_dir / "concepts" / f"{slug}.md").write_text(
                _FM.format(title=title) + f"# {title}\n\n## {title}主题\n\n内容足够长供维护使用。\n",
                encoding="utf-8",
            )
        self.issue_store = make_issue_store(self.workspace)
        self.issue_service = IssueService(self.issue_store)
        self.job_service = make_job_service(
            self.workspace,
            wiki_dir=self.wiki_dir,
            materials_dir=self.materials_dir,
            sync_state=SyncState(self.workspace / "watch" / "state.json"),
        )
        self.job_worker = JobWorker(self.job_service)
        self.agent = SimpleNamespace(llm=None)
        self.issue_actions = IssueActionExecutor(cast(AppRuntime, self))
        register_job_handlers(self.job_worker, self.issue_actions)
        self.wiki_browser = WikiBrowser(
            wiki_dir=self.wiki_dir,
            source_records_dir=self.workspace / "provenance" / "sources",
            issue_store=self.issue_service.store if hasattr(self.issue_service, "store") else None,
            project_root=root,
        )
        self.session = SessionService(
            agent=cast(ReActAgent, object()),
            session_manager=SessionManager(workspace=self.workspace),
            event_publisher=EventPublisher(),
        )
        self._worker_task = None

    async def __aenter__(self):
        self._worker_task = asyncio.create_task(self.job_worker.run())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.job_worker.stop()
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)


def _client(runtime: _Runtime, tmp_path: Path):
    app = create_app(project_root=tmp_path, runtime=cast(AppRuntime, runtime))

    async def go(fn):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await fn(client)

    return go


def test_maintenance_submit_returns_batch_with_readable_titles(tmp_path: Path):
    runtime = _Runtime(tmp_path)

    async def fn(client):
        r = await client.post(
            "/api/maintenance",
            json={"units": [{"in_pages": ["concepts/a", "concepts/b"],
                             "out": [{"slug": "concepts/a", "intent": "合并页", "polish": True}],
                             "reason": "重复主题"}]},
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["count"] == len(body["tasks"]) == 2  # 1 单元 + 批尾补链 a
        assert body["batch"].startswith("restructure_")
        kinds = [t["kind"] for t in body["tasks"]]
        assert kinds == [Kind.RESTRUCTURE, Kind.LINK]
        assert body["tasks"][0]["title"].startswith("重组 concepts/a+concepts/b")
        assert body["tasks"][1]["title"] == "补链 concepts/a"

    asyncio.run(_client(runtime, tmp_path)(fn))


def test_maintenance_submit_error_mapping(tmp_path: Path):
    runtime = _Runtime(tmp_path)

    async def fn(client):
        # 空清单 → 400
        r = await client.post("/api/maintenance", json={"units": []})
        assert r.status_code == 400
        # 单元落不上盘面（in 不存在）→ 400
        r = await client.post(
            "/api/maintenance",
            json={"units": [{"in_pages": ["concepts/missing"], "out": [], "reason": "x"}]},
        )
        assert r.status_code == 400
        # 写任务在途 → 409
        runtime.job_service.submit(kind=Kind.COMPILE, resource="/x", mode="sync")
        r = await client.post(
            "/api/maintenance",
            json={"units": [{"in_pages": ["concepts/a"], "out": [], "reason": "删"}]},
        )
        assert r.status_code == 409

    asyncio.run(_client(runtime, tmp_path)(fn))


def test_link_submit_roster_and_busy(tmp_path: Path):
    runtime = _Runtime(tmp_path)

    async def fn(client):
        r = await client.post("/api/link", json={"slugs": ["concepts/ghost"]})
        assert r.status_code == 400
        r = await client.post("/api/link", json={"slugs": ["concepts/a"]})
        assert r.status_code == 202
        assert r.json()["count"] == 1 and r.json()["tasks"][0]["title"] == "补链 concepts/a"
        # 全库默认批含同一页——在途 link 行挡新批（互斥闸语义）
        r = await client.post("/api/link", json={})
        assert r.status_code == 409

    asyncio.run(_client(runtime, tmp_path)(fn))


def test_maintenance_preview_enqueue_converge_and_gate(tmp_path: Path):
    """预览已任务化：202 入队、重复发起收敛为同一在途、写在途时 409。"""
    runtime = _Runtime(tmp_path)

    async def fn(client):
        r = await client.post("/api/maintenance/preview")
        assert r.status_code == 202, r.text
        task = r.json()["task"]
        assert task["kind"] == "maintenance_preview"
        assert task["title"] == "整理结构分析"
        assert task["status"] == "queued"

        r2 = await client.post("/api/maintenance/preview")
        assert r2.status_code == 202
        assert r2.json()["task"]["id"] == task["id"]  # 幂等键合并，不排第二份

        runtime.job_service.submit(kind=Kind.DELETE, resource="/gone", mode="sync")
        r3 = await client.post("/api/maintenance/preview")
        assert r3.status_code == 409 and "在途" in r3.json()["detail"]

    asyncio.run(_client(runtime, tmp_path)(fn))


def test_preview_handler_payload_stages_and_result(tmp_path: Path, monkeypatch):
    """handler 走三阶段 progress、建议清单进 JobResult；service 把结果落库。"""
    import wiki_agent.application.restructure_service as svc_mod
    from wiki_agent.application.wiki_ops import WikiOpsHandler

    runtime = _Runtime(tmp_path)
    unit = Unit(in_pages=["concepts/a", "concepts/b"], out=[], reason="整体清理")
    outcome = MaintenanceOutcome(
        proposed=[unit], confirmed=[unit], effective=[unit], rejected=[(unit, "示例放弃")]
    )
    seen_stages: list[str] = []

    async def fake_propose(llm, wiki_dir, *, confirm=None, progress=None):
        for stage in ("初步建议", "二次复核", "冲突消解"):
            if progress:
                progress(stage)
        return outcome

    monkeypatch.setattr(svc_mod, "propose_maintenance", fake_propose)

    handler = WikiOpsHandler(
        None, wiki_dir=runtime.wiki_dir, source_records_dir=runtime.workspace / "provenance"
    )
    job = runtime.job_service.submit_maintenance_preview()

    async def main():
        result = await handler.handle_preview(job, seen_stages.append)
        assert result.status == "succeeded"
        preview = result.detail["preview"]
        assert preview["proposed"] == 1 and preview["confirmed"] == 1
        assert preview["effective"][0]["in_pages"] == ["concepts/a", "concepts/b"]
        assert preview["rejected"][0]["reason"] == "示例放弃"

        # 队列卡片阶段文本按 progress 更新；终态 result 随 job 持久化
        claimed = runtime.job_service.claim_next(kinds={"maintenance_preview"})
        assert claimed is not None and claimed.id == job.id
        runtime.job_service.mark_stage(job.id, "二次复核")
        done = runtime.job_service.complete_with_outcome(claimed, result)
        assert done.stage == "done"
        assert done.result["preview"]["effective"][0]["reason"] == "整体清理"
        assert done.result["preview"]["healthy"] is False

    asyncio.run(main())
    assert seen_stages == ["初步建议", "二次复核", "冲突消解"]


def test_maintenance_preview_resolve_flow(tmp_path: Path):
    """处置（否决/已入队）标记持久化；非法对象与非法方式拒绝、未知任务 404。"""
    from wiki_agent.jobs import JobResult
    from wiki_agent.jobs import Kind as JKind

    runtime = _Runtime(tmp_path)

    async def fn(client):
        job = runtime.job_service.submit_maintenance_preview()
        claimed = runtime.job_service.claim_next(kinds={JKind.MAINTENANCE_PREVIEW})
        assert claimed is not None
        runtime.job_service.complete_with_outcome(
            claimed,
            JobResult(status="succeeded", detail={"preview": {"healthy": False, "effective": []}}),
        )

        other = runtime.job_service.submit(kind=JKind.DELETE, resource="/x", mode="sync")
        r = await client.post(
            f"/api/maintenance/preview/{other.id}/resolve", json={"by": "dismissed"}
        )
        assert r.status_code == 400  # 只能处置分析结果

        r = await client.post(
            f"/api/maintenance/preview/{job.id}/resolve", json={"by": "unknown_way"}
        )
        assert r.status_code in (400, 422)  # 枚举外的处置方式（pydantic 422/service 400）

        r = await client.post(
            "/api/maintenance/preview/no_such_job/resolve", json={"by": "dismissed"}
        )
        assert r.status_code == 404

        r = await client.post(
            f"/api/maintenance/preview/{job.id}/resolve", json={"by": "submitted"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["task"]["result"]["preview"]["resolved_by"] == "submitted"
        # 标记随 job 行持久：重新读取仍是已处置
        again = runtime.job_service.get(job.id)
        assert again.result["preview"]["resolved_by"] == "submitted"
        assert again.status == "succeeded"  # 状态不动，只改结果标记（completed 是 HTTP 层文案）

    asyncio.run(_client(runtime, tmp_path)(fn))


def test_new_preview_supersedes_previous_settled(tmp_path: Path):
    """不变量：发起新一轮即作废上一轮——未处置的终态分析行 ≤1。

    界面结果行读侧不需要"哪条算最新"判断的底气来自这里；历史轮的
    处置记录（superseded）随各行持久，可审计。作废只扫 preview 的
    终态行，不碰其他 kind。
    """
    from wiki_agent.jobs import JobResult, Kind

    runtime = _Runtime(tmp_path)
    first = runtime.job_service.submit_maintenance_preview()
    claimed = runtime.job_service.claim_next(kinds={Kind.MAINTENANCE_PREVIEW})
    assert claimed is not None and claimed.id == first.id
    runtime.job_service.complete_with_outcome(
        claimed,
        JobResult(status="succeeded", detail={"preview": {"healthy": False, "effective": []}}),
    )
    # 非 preview 的终态行不能被作废扫到——先推到终态（在途会触发写在途闸）
    other = runtime.job_service.submit(kind=Kind.DELETE, resource="/d", mode="sync")
    claimed_delete = runtime.job_service.claim_next(kinds={Kind.DELETE})
    assert claimed_delete is not None and claimed_delete.id == other.id
    runtime.job_service.complete_with_outcome(
        claimed_delete, JobResult(status="failed", detail={"error": "x"})
    )

    second = runtime.job_service.submit_maintenance_preview()

    again = runtime.job_service.get(first.id)
    assert again.result["preview"]["resolved_by"] == "superseded"
    fresh = runtime.job_service.get(second.id)
    assert "resolved_by" not in (fresh.result.get("preview") or {})
    assert runtime.job_service.get(other.id).result == {"error": "x"}
