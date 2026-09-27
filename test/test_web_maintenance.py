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


def test_maintenance_preview_shape_and_gate(tmp_path: Path, monkeypatch):
    import wiki_agent.application.restructure_service as svc_mod

    runtime = _Runtime(tmp_path)
    unit = Unit(in_pages=["concepts/a", "concepts/b"], out=[], reason="整体清理")
    outcome = MaintenanceOutcome(
        proposed=[unit], confirmed=[unit], effective=[unit],
        rejected=[(unit, "示例放弃")],
    )

    async def fake_propose(llm, wiki_dir, *, confirm=None):
        return outcome

    monkeypatch.setattr(svc_mod, "propose_maintenance", fake_propose)

    async def fn(client):
        r = await client.post("/api/maintenance/preview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["proposed"] == 1 and body["confirmed"] == 1
        assert body["effective"][0]["in_pages"] == ["concepts/a", "concepts/b"]
        assert body["effective"][0]["out"] == []
        assert body["rejected"][0]["reason"] == "示例放弃"
        # 闸：在途时 preview 直接 409，不进 LLM 分析
        runtime.job_service.submit(kind=Kind.DELETE, resource="/gone", mode="sync")
        r = await client.post("/api/maintenance/preview")
        assert r.status_code == 409 and "在途" in r.json()["detail"]

    asyncio.run(_client(runtime, tmp_path)(fn))
