"""周期对账——事件通道会丢，真相以库为准。

三件事（全部幂等）：
1. recover_stale：超时未心跳的 running 回队（进程崩溃/卡死自愈）；
2. 孤儿 processing：issue 挂在 processing 却没有在途 job（终态事务崩在
   中间）→ CAS 回落 open，重走提交入口；
3. 补挂关系：升级前遗留的无 issue_id 在途 compile 与 open 失败账对上，
   让终态联动能找到账本。

对账只做 SQLite 状态修复，永不触碰 pipeline/文件。
"""

from __future__ import annotations

import asyncio

from wiki_agent.application.job_service import JobService
from wiki_agent.issues import InvalidIssueTransitionError, IssueAlreadyClaimedError, IssueStatus
from wiki_agent.log import get_logger

logger = get_logger("RECONCILE")


class Reconciler:
    def __init__(
        self, job_service: JobService, *, interval: float = 60.0, max_age_seconds: int = 300
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
                logger.warning("对账轮次失败: %s: %s", type(exc).__name__, str(exc)[:200])

    def stop(self) -> None:
        self._stopped.set()

    def run_once(self) -> dict[str, int]:
        store = self._service.store
        issues = self._service.issues
        recovered = store.recover_stale(max_age_seconds=self._max_age)

        orphans = 0
        for issue in issues.list(statuses={IssueStatus.PROCESSING}, limit=1000):
            if store.has_active_job_by_issue(issue.id):
                continue
            try:
                issues.transition(
                    issue.id,
                    IssueStatus.OPEN,
                    resolution={"reason": "reconcile: no active job"},
                    expected={IssueStatus.PROCESSING},
                    event="reconcile",
                )
                orphans += 1
            except (IssueAlreadyClaimedError, InvalidIssueTransitionError):
                continue

        relinked = 0
        for job in store.list_active_without_issue():
            pending = issues.find_pending_failures(job.resource)
            if not pending:
                continue
            store.attach_issue(job.id, pending[0].id)
            relinked += 1

        result = {"recovered": recovered, "orphans": orphans, "relinked": relinked}
        if any(result.values()):
            logger.info("对账: %s", result)
        return result
