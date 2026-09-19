"""到期 source 失败的重试调度器——只挑选、只提交 Job，永不执行流水线。

与 watcher 事件通道是同一提交入口（submit_issue_retry）的两种触发器：
文件再变靠事件加速，无人介入的到期退避靠这里定时。执行与终态全部
归 Worker + JobOutcomeHandler，本模块零副作用于 pipeline/Git。
"""

from __future__ import annotations

import asyncio

from wiki_agent.application.job_service import JobService
from wiki_agent.compiler.workflows.failures import (
    is_retry_due,
    source_retry_decision,
)
from wiki_agent.issues import IssueAlreadyClaimedError, IssueKind, IssueStatus
from wiki_agent.jobs import DuplicateActiveJob
from wiki_agent.log import get_logger

logger = get_logger("RETRY_SCHEDULER")


class RetryScheduler:
    def __init__(self, job_service: JobService, *, interval: float = 30.0):
        self._service = job_service
        self._interval = interval
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
                continue  # stop() 唤醒即退出
            except TimeoutError:
                pass
            await self.run_due()

    def stop(self) -> None:
        self._stopped.set()

    async def run_due(self) -> int:
        """把到期且还值得重试的失败问题排队成 compile job。"""
        submitted = 0
        max_attempts = self._service.retry_config.source_max_attempts
        issues = self._service.issues.list(
            statuses={IssueStatus.OPEN}, kinds={IssueKind.INGESTION_FAILURE}, limit=1000
        )
        for issue in issues:
            if issue.retry.get("unavailable_reason"):
                continue
            if source_retry_decision(issue.retry, max_attempts=max_attempts) != "retry":
                continue
            if not is_retry_due(issue.retry):
                continue
            if self._service.store.has_active_job_by_issue(issue.id):
                continue
            try:
                self._service.submit_issue_retry(issue.id)
            except (IssueAlreadyClaimedError, DuplicateActiveJob) as exc:
                logger.debug("重试调度让位在途任务 %s: %s", issue.id, exc)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余调度
                logger.warning("重试调度 %s 失败: %s: %s", issue.id, type(exc).__name__, exc)
                continue
            submitted += 1
        if submitted:
            logger.info("重试调度：本轮排队 %d 个 job", submitted)
        return submitted
