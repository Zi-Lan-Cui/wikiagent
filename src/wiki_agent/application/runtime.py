"""Application runtime: the single composition root for a Wiki Agent process.

统一执行模型的装配点：Job 队列 + Worker（唯一的执行后台循环）在这里组装。
崩溃自愈不靠常驻调度器：JobService 构造即 recover_stale，sync 互斥闸保证
队列排空前不开新快照。宿主进程 start() 即拥有执行能力——jobs 表是唯一
队列，谁领取都收敛。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Any

from wiki_agent.agent import ReActAgent
from wiki_agent.application.wiki_ops import WikiOpsConsumer
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.config import RootConfig, load_config
from wiki_agent.events import AgentHook, EventPublisher
from wiki_agent.exec_lock import acquire_execution_lock, release_execution_lock
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.hooks import IssueReporterHook
from wiki_agent.jobs.service import JobService
from wiki_agent.jobs.worker import JobWorker
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.sync.job_consumer import SyncConsumer
from wiki_agent.sync.state import SyncState
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry
from wiki_agent.versioning import WikiGitManager


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
        self.issue_store = IssueStore(self.workspace)
        self.sync_state = SyncState(config.paths.resolved_sync_dir() / "state.json")
        # wiki 版本面：HEAD=最近已结算状态，sync/retry 逐 job 提交由 consumer 执行
        self.git_manager = WikiGitManager(self.wiki_dir)
        self.job_service = JobService(
            self.workspace,
            wiki_dir=self.wiki_dir,
            sync_state=self.sync_state,
            source_records_dir=self.source_records_dir,
        )
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
            git=self.git_manager,
        )
        # refine 是 wiki 自编译——mode=refine 的流水线（index 排他、不存档案页）
        self.refine_pipeline = CompilePipeline(
            llm=self.agent.llm,
            vlm=self.agent.vlm,
            wiki_dir=self.wiki_dir,
            mode="refine",
            compile_config=config.compile,
        )
        self.wiki_ops = WikiOpsConsumer(
            self.refine_pipeline,
            wiki_dir=self.wiki_dir,
            source_records_dir=self.source_records_dir,
            git=self.git_manager,
        )
        self.job_worker = JobWorker(self.job_service)
        self.job_worker.register("compile", self.sync_consumer.handle_job)
        self.job_worker.register("delete", self.sync_consumer.handle_job)
        self.job_worker.register("refine", self.wiki_ops.handle_refine)
        self.job_worker.register("restructure", self.wiki_ops.handle_restructure)
        self._mcp_connections: dict[str, Any] = {}
        self._bg_tasks: list[asyncio.Task] = []
        self._started = False
        self._exec_lock_held = False

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
        """MCP 连接 + 执行后台循环（worker/维护循环）一次性拉起。

        start = 宣布本进程为执行者：先拿执行锁（git 协议要求 wiki 写者唯一），
        他进程持有时直接失败——不带病启动。
        """
        if self._started:
            return
        acquire_execution_lock(self.workspace)
        self._exec_lock_held = True
        if self.config.mcp.servers:
            from wiki_agent.tools.mcp_adaptor import connect_mcp_servers

            self._mcp_connections = await connect_mcp_servers(
                self.config.mcp.servers,
                self.tool_registry,
            )
        self._bg_tasks = [
            asyncio.create_task(self.job_worker.run(), name="wiki-runtime:job-worker"),
        ]
        self._started = True

    async def close(self) -> None:
        """停后台循环、关 MCP、释放执行锁与 runtime 资源。"""
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
        if self._exec_lock_held:
            release_execution_lock(self.workspace)
            self._exec_lock_held = False

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
