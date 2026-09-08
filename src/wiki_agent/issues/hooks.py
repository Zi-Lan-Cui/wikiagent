"""Agent lifecycle integration for the unified issue center."""

from __future__ import annotations

import asyncio

from wiki_agent.events import AgentHook, RunContext
from wiki_agent.issues.producers import report_run_failure
from wiki_agent.issues.service import IssueService


class IssueReporterHook(AgentHook):
    """Persist fatal Agent turns without coupling the agent to SQLite."""

    def __init__(self, service: IssueService):
        super().__init__()
        self._service = service

    async def on_run_error(self, context: RunContext) -> None:
        if isinstance(context.exception, asyncio.CancelledError):
            return
        error = (
            context.exception
            if isinstance(context.exception, Exception)
            else RuntimeError(context.error or "Agent 运行失败")
        )
        report_run_failure(
            self._service,
            title="问答运行失败",
            error=error,
            origin={
                "run_id": context.run_id,
                "session_id": context.session_key,
                "stage": "agent_run",
            },
        )
