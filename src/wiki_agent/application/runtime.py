"""Application runtime: the single composition root for a Wiki Agent process.

统一执行模型的装配点：Job 队列 + Worker（唯一的执行后台循环）在这里组装。
崩溃自愈不靠常驻调度器：JobService 构造即 recover_stale，sync 互斥闸保证
队列排空前不开新快照。宿主进程 start() 即拥有执行能力——jobs 表是唯一
队列，谁领取都收敛。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from wiki_agent.agent import ReActAgent
from wiki_agent.application.job_service import JobService
from wiki_agent.application.job_worker import JobWorker
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.config import RootConfig, load_config
from wiki_agent.events import AgentHook, EventPublisher
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.hooks import IssueReporterHook
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.sync.job_consumer import SyncConsumer
from wiki_agent.sync.state import SyncState
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry


class AppRuntime:
    """Own the process-wide dependencies used by CLI and future Web adapters.

    A runtime is intentionally single-process scoped.  Construct one instance
    at application startup and reuse it for all requests in that process.
    """

    def __init__(self, config: RootConfig, *, hooks: list[AgentHook] | None = None) -> None:
        self.config = config
        self.materials_dir = config.paths.resolved_materials_dir()
        self.workspace = config.paths.resolved_workspace_dir()
        self.wiki_dir = config.paths.resolved_wiki_dir()
        self.source_records_dir = config.paths.resolved_source_records_dir()
        self.runs_dir = config.paths.resolved_runs_dir()
        self.issue_store = IssueStore(self.workspace)
        self.sync_state = SyncState(config.paths.resolved_sync_dir() / "state.json")
        self.job_service = JobService(
            self.workspace,
            wiki_dir=self.wiki_dir,
            sync_state=self.sync_state,
        )
        self._migrate_legacy_retry_rows()
        self.issue_service = IssueService(self.issue_store)
        self.event_publisher = EventPublisher()
        self.issue_reporter = IssueReporterHook(self.issue_service)
        self.tool_registry = ToolRegistry()
        self.tool_registry.register(ReadFile(self.wiki_dir, workspace=self.workspace))
        self.tool_registry.register(ListDir(self.wiki_dir))
        self.tool_registry.register(Grep(self.wiki_dir))
        self.agent = ReActAgent(
            name="wiki-qa",
            llm=create_llm(config.llm, config.retry),
            vlm=create_vlm(config.vlm, config.retry),
            tool_registry=self.tool_registry,
            workspace=self.workspace,
            wiki_dir=self.wiki_dir,
            agent_config=config.agent,
            compile_config=config.compile,
            retry_config=config.retry,
            issue_service=self.issue_service,
            job_service=self.job_service,
            hooks=[self.event_publisher, self.issue_reporter, *(hooks or [])],
        )
        # 执行装配：worker 是唯一终态写入者；装配根注册 compile/delete，
        # issue_action 由 web 适配器补挂，多进程共库按 kinds 分工。
        self.pipeline = CompilePipeline(
            llm=self.agent.llm,
            vlm=self.agent.vlm,
            wiki_dir=self.wiki_dir,
            source_records_dir=self.source_records_dir,
            compile_config=config.compile,
        )
        self.sync_consumer = SyncConsumer(
            self.pipeline,
            self.sync_state,
            wiki_dir=self.wiki_dir,
            source_records_dir=self.source_records_dir,
        )
        self.job_worker = JobWorker(self.job_service)
        self.job_worker.register("compile", self.sync_consumer.handle_job)
        self.job_worker.register("delete", self.sync_consumer.handle_job)
        self._mcp_connections: dict[str, Any] = {}
        self._bg_tasks: list[asyncio.Task] = []
        self._started = False

    def _migrate_legacy_retry_rows(self) -> None:
        """一次性迁移：旧"委托壳"retry 在途行让位。

        账本删除后 retry 直投 compile job；遗留的 mode=="retry" issue_action
        行已无执行语义，取消之（rescan 行仍可正常执行，保留）。meta 标志幂等。
        """
        if self.issue_store.get_meta("issue_retry_direct_v3"):
            return
        with self.job_service.store.database.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='cancelled', stage='cancelled',"
                " error='迁移：retry 已改为直投 compile job', updated_at=?"
                " WHERE kind='issue_action' AND mode='retry' AND status IN ('queued','running')",
                (datetime.now(UTC).isoformat(),),
            )
            cancelled = cur.rowcount
        self.issue_store.set_meta("issue_retry_direct_v3", "1")
        if cancelled:
            from wiki_agent.log import get_logger

            get_logger("RUNTIME").info("迁移取消委托壳 retry 在途行 %d 个", cancelled)

    @classmethod
    def from_project_root(
        cls,
        project_root: Path,
        *,
        debug: bool = False,
        hooks: list[AgentHook] | None = None,
    ) -> AppRuntime:
        """Load project configuration and construct a ready runtime."""
        config = load_config(
            project_root=project_root,
            overrides={"logging": {"debug": debug}},
        )
        return cls(config, hooks=hooks)

    async def start(self) -> None:
        """MCP 连接 + 执行后台循环（worker/维护循环）一次性拉起。"""
        if self._started:
            return
        if self.config.mcp.servers:
            from wiki_agent.tools.mcp_tools.mcp_adaptor import connect_mcp_servers

            self._mcp_connections = await connect_mcp_servers(
                self.config.mcp.servers,
                self.tool_registry,
            )
        self._bg_tasks = [
            asyncio.create_task(self.job_worker.run(), name="wiki-runtime:job-worker"),
        ]
        self._started = True

    async def close(self) -> None:
        """停后台循环、关 MCP、释放 runtime 资源。"""
        self.job_worker.stop()
        tasks, self._bg_tasks = self._bg_tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        connections, self._mcp_connections = self._mcp_connections, {}
        self._started = False
        for connection in connections.values():
            await connection.aclose()

    async def __aenter__(self) -> AppRuntime:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()
