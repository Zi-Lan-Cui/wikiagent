"""后台维护循环——事件通道会丢，真相以库为准，周期收敛一切脱缝。

每轮三件事（全部幂等、只生产不执行）：
1. recover_stale：超时未心跳的 running 回队（进程崩溃/卡死自愈）；
2. 到期重试：到退避时间且仍值得自动重试的失败账 → submit_issue_retry 排队
   ——与 watcher 事件是同一提交入口的两种触发器；
3. 补挂关系：无 issue_id 的在途 compile 与 open 失败账对上，终态联动找得到账本。

"在途"没有需要修复的镜像状态——它就是 jobs 表的实时形状；孤儿
processing 这一整类崩溃残缝随 issue_actions 账本一起消失。

只做 SQLite 状态修复与 Job 提交，永不触碰 pipeline/文件；执行与终态
归 Worker + JobOutcomeHandler。
"""

from __future__ import annotations

import asyncio

from wiki_agent.application.job_service import JobService
from wiki_agent.compiler.workflows.failures import (
    is_retry_due,
    source_retry_decision,
)
from wiki_agent.compiler.workflows.retry import SourceUnavailableError
from wiki_agent.issues import IssueKind, IssueStatus
from wiki_agent.log import get_logger

logger = get_logger("RECONCILE")


class MaintenanceLoop:
    """单一后台生产者：对账 + 到期重试，一个周期、一份顺序。"""

    def __init__(
        self, job_service: JobService, *, interval: float = 30.0, max_age_seconds: int = 300
    ):
        self._service = job_service
        self._interval = interval
        self._max_age = max_age_seconds
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
                return
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as exc:  # noqa: BLE001 - 单轮失败等下一轮
                logger.warning("维护轮次失败: %s: %s", type(exc).__name__, str(exc)[:200])

    def stop(self) -> None:
        self._stopped.set()

    def run_once(self) -> dict[str, int]:
        store = self._service.store
        issues = self._service.issues
        result = {"recovered": store.recover_stale(max_age_seconds=self._max_age)}

        result["retry_submitted"] = self._submit_due_retries()

        result["relinked"] = 0
        for job in store.list_active_without_issue():
            pending = issues.find_pending_failures(job.resource)
            if not pending:
                continue
            store.attach_issue(job.id, pending[0].id)
            result["relinked"] += 1

        if any(result.values()):
            logger.info("维护: %s", result)
        return result

    def _submit_due_retries(self) -> int:
        """把到期且还值得重试的失败问题排队成 compile job。"""
        service = self._service
        submitted = 0
        max_attempts = service.retry_config.source_max_attempts
        issues = service.issues.list(
            statuses={IssueStatus.OPEN}, kinds={IssueKind.INGESTION_FAILURE}, limit=1000
        )
        for issue in issues:
            if issue.retry.get("unavailable_reason"):
                continue
            if source_retry_decision(issue.retry, max_attempts=max_attempts) != "retry":
                continue
            if not is_retry_due(issue.retry):
                continue
            if service.store.has_active_job_by_issue(issue.id):
                continue
            try:
                service.submit_issue_retry(issue.id)
            except (SourceUnavailableError, LookupError, ValueError) as exc:
                logger.debug("重试调度跳过 %s: %s", issue.id, exc)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余调度
                logger.warning("重试调度 %s 失败: %s: %s", issue.id, type(exc).__name__, exc)
                continue
            submitted += 1
        return submitted
