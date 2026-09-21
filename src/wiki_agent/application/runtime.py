"""Application runtime: the single composition root for a Wiki Agent process.

统一执行模型的装配点：Job 队列 + Worker + MaintenanceLoop（对账/到期重试）
在这里组装。宿主进程（web serve / watch 脚本）start() 即拥有完整后台循环；
纯 CLI 交互进程 start() 同样安全——jobs 表是唯一队列，谁领取都收敛。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Any

from wiki_agent.agent import ReActAgent
from wiki_agent.application.job_service import JobService
from wiki_agent.application.job_worker import JobWorker
from wiki_agent.application.reconcile import MaintenanceLoop
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.config import RootConfig, load_config
from wiki_agent.events import AgentHook, EventPublisher
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.hooks import IssueReporterHook
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry
from wiki_agent.watch.consumer import WatchConsumer
from wiki_agent.watch.state import WatchState


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
        self.watch_state = WatchState(config.paths.resolved_watch_dir() / "state.json")
        self.job_service = JobService(
            self.workspace,
            retry_config=config.retry,
            wiki_dir=self.wiki_dir,
            watch_state=self.watch_state,
        )
        self._migrate_issue_action_rows()
        self.issue_service = IssueService(self.issue_store)
        self.interrupted_issue_actions = self.issue_store.recover_interrupted_actions()
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
        # —— 执行装配：worker 是唯一终态写入者，三 handler 全注册，
        #    多进程共库按 kinds 分工，不存在"谁误领谁"的问题。
        self.pipeline = CompilePipeline(
            llm=self.agent.llm,
            vlm=self.agent.vlm,
            wiki_dir=self.wiki_dir,
            source_records_dir=self.source_records_dir,
            compile_config=config.compile,
        )
        self.watch_consumer = WatchConsumer(
            self.pipeline,
            self.watch_state,
            wiki_dir=self.wiki_dir,
            source_records_dir=self.source_records_dir,
        )
        self.job_worker = JobWorker(self.job_service)
        self.job_worker.register("compile", self.watch_consumer.handle_job)
        self.job_worker.register("delete", self.watch_consumer.handle_job)
        self.maintenance = MaintenanceLoop(self.job_service)
        self._mcp_connections: dict[str, Any] = {}
        self._bg_tasks: list[asyncio.Task] = []
        self._started = False

    def _migrate_issue_action_rows(self) -> None:
        """一次性迁移（统一执行模型 Step5）：旧格式在途 issue_action 行让位。

        旧模型的 retry/rescan issue_action 可能残留 queued/running——新模型
        里重试以 compile job 表达；meta 标志保证只跑一次，下个版本删代码。
        """
        if self.issue_store.get_meta("issue_action_jobs_v2"):
            return
        cancelled = self.job_service.store.cancel_queued_running(
            "issue_action", reason="统一执行模型迁移：重试改由 compile job 表达"
        )
        self.issue_store.set_meta("issue_action_jobs_v2", "1")
        if cancelled:
            from wiki_agent.log import get_logger

            get_logger("RUNTIME").info("迁移取消旧格式 issue_action 在途行 %d 个", cancelled)

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
            asyncio.create_task(self.maintenance.run(), name="wiki-runtime:maintenance"),
        ]
        self._started = True

    async def close(self) -> None:
        """停后台循环、关 MCP、释放 runtime 资源。"""
        self.job_worker.stop()
        self.maintenance.stop()
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
