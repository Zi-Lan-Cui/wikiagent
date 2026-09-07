"""Application runtime: the single composition root for a Wiki Agent process."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

from wiki_agent.agent import ReActAgent
from wiki_agent.application.events import EventPublisher
from wiki_agent.application.job_service import JobService
from wiki_agent.config import RootConfig, load_config
from wiki_agent.hook import AgentHook
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.hooks import IssueReporterHook
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry


class AppRuntime:
    """Own the process-wide dependencies used by CLI and future Web adapters.

    A runtime is intentionally single-process scoped.  Construct one instance
    at application startup and reuse it for all requests in that process.
    """

    def __init__(self, config: RootConfig, *, hooks: list[AgentHook] | None = None) -> None:
        self.config = config
        self.source_dir = config.paths.resolved_source_dir()
        self.workspace = config.paths.resolved_workspace_dir()
        self.wiki_dir = config.paths.resolved_wiki_dir()
        self.source_records_dir = config.paths.resolved_source_records_dir()
        self.runs_dir = config.paths.resolved_runs_dir()
        self.issue_store = IssueStore(self.workspace)
        self.job_service = JobService(self.workspace)
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
            hooks=[self.event_publisher, self.issue_reporter, *(hooks or [])],
        )
        self._mcp_connections: dict[str, Any] = {}
        self._started = False

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
        """Start optional MCP connections once for this process."""
        if self._started:
            return
        if self.config.mcp.servers:
            from wiki_agent.tools.mcp_tools.mcp_adaptor import connect_mcp_servers

            self._mcp_connections = await connect_mcp_servers(
                self.config.mcp.servers,
                self.tool_registry,
            )
        self._started = True

    async def close(self) -> None:
        """Close MCP connections and release runtime-owned resources."""
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
