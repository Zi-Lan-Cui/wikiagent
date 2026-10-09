"""业务线提交入口的过渡壳：所有线提交与机制都委托出去，壳只做同名转发。

三条线的提交规则住在各自领域包：sync/coordinator、maintenance/coordinator、
issues/coordinator；行生命周期在 queue/queue。壳的存在只为兼容尚未改接的调用方。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.issues import IssueStore
from wiki_agent.queue import Job, JobResult
from wiki_agent.queue.queue import JobQueue

if TYPE_CHECKING:
    from wiki_agent.issues.coordinator import IssueCoordinator
    from wiki_agent.maintenance import ProposalStore
    from wiki_agent.maintenance.coordinator import MaintenanceCoordinator
    from wiki_agent.sync.baseline import SyncBaseline
    from wiki_agent.sync.coordinator import SyncCoordinator


class JobService:
    """提交口壳：机制委托 JobQueue，线提交委托各线 coordinator。"""

    def __init__(
        self,
        *,
        queue: JobQueue,
        issues: IssueStore,
        sync: SyncCoordinator | None = None,
        maintenance: MaintenanceCoordinator | None = None,
        issues_coordinator: IssueCoordinator | None = None,
        baseline: SyncBaseline | None = None,
        proposals: ProposalStore | None = None,
    ):
        self.queue = queue
        # 提交口与测试读模型共用同一份底层对象，壳只转发不另持引用
        self.store = queue.store
        self.snapshots = queue.snapshots
        self.outcomes = queue.outcomes
        self.issues = issues
        self.sync = sync
        self.maintenance = maintenance
        self.issues_coordinator = issues_coordinator
        # 基线落后集合的只读入口，供维护流程预检与脚本消费
        self.baseline = baseline
        # 提议账本只读入口：web 处置端点经此查询与否决，入账在 coordinator 事务内
        self.proposals = proposals

    def recover_stale(self) -> int:
        return self.queue.recover_stale()

    @property
    def recovered_jobs(self) -> int:
        return self.queue.recovered_jobs

    def sync_baseline_lag(self) -> set[str]:
        """基线落后集合；未注入基线面时返回空集。"""
        if self.baseline is None:
            return set()
        return self.baseline.lagging_sources()

    def submit_sync(self, source_dir: str | Path) -> list[Job]:
        """委托 sync 线提交口；未注入即拒——快照与入队规则在 SyncCoordinator。"""
        if self.sync is None:
            raise RuntimeError("submit_sync 需要注入 sync coordinator")
        return self.sync.submit_sync(source_dir)

    def sync_status(self, source_dir: str | Path) -> dict[str, int]:
        if self.sync is None:
            raise RuntimeError("sync_status 需要注入 sync coordinator")
        return self.sync.sync_status(source_dir)

    def submit_restructure(self, units: list[dict], proposal_id: str = "") -> list[Job]:
        """委托维护线提交口；闸、校验与账本挂账在 MaintenanceCoordinator。"""
        if self.maintenance is None:
            raise RuntimeError("submit_restructure 需要注入 maintenance coordinator")
        return self.maintenance.submit_restructure(units, proposal_id)

    def submit_link_batch(self, slugs: list[str] | None = None) -> list[Job]:
        if self.maintenance is None:
            raise RuntimeError("submit_link_batch 需要注入 maintenance coordinator")
        return self.maintenance.submit_link_batch(slugs)

    def submit_restructure_preview(self) -> Job:
        if self.maintenance is None:
            raise RuntimeError("submit_restructure_preview 需要注入 maintenance coordinator")
        return self.maintenance.submit_restructure_preview()

    def submit_issue_retry(self, issue_id: str) -> Job:
        """委托 issues 线提交口；资格、收敛与入队规则在 IssueCoordinator。"""
        if self.issues_coordinator is None:
            raise RuntimeError("submit_issue_retry 需要注入 issues coordinator")
        return self.issues_coordinator.submit_issue_retry(issue_id)

    def submit_issue_retry_batch(self, issue_ids: list[str]) -> list[Job]:
        if self.issues_coordinator is None:
            raise RuntimeError("submit_issue_retry_batch 需要注入 issues coordinator")
        return self.issues_coordinator.submit_issue_retry_batch(issue_ids)

    def mark_retry_unavailable(self, issue_id: str, reason: str) -> None:
        if self.issues_coordinator is None:
            raise RuntimeError("mark_retry_unavailable 需要注入 issues coordinator")
        self.issues_coordinator.mark_retry_unavailable(issue_id, reason)

    def submit_issue_action(
        self, issue_id: str, action: str, payload: dict[str, object] | None = None
    ) -> Job:
        if self.issues_coordinator is None:
            raise RuntimeError("submit_issue_action 需要注入 issues coordinator")
        return self.issues_coordinator.submit_issue_action(issue_id, action, payload)

    def submit(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        issue_id: str = "",
    ) -> Job:
        return self.queue.submit(
            kind=kind,
            resource=resource,
            mode=mode,
            payload=payload,
            idempotency_key=idempotency_key,
            issue_id=issue_id,
        )

    # 执行生命周期与只读视图：全部委托 JobQueue

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        return self.queue.claim_next(kinds=kinds)

    def mark_stage(self, job_id: str, stage: str) -> Job:
        return self.queue.mark_stage(job_id, stage)

    def complete_with_outcome(self, job: Job, result: JobResult) -> Job:
        return self.queue.complete_with_outcome(job, result)

    def cancel_terminal(self, job: Job) -> None:
        self.queue.cancel_terminal(job)

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.queue.list(limit=limit)

    def get(self, job_id: str) -> Job:
        return self.queue.get(job_id)

    def count_in_flight(self) -> int:
        return self.queue.count_in_flight()

    def in_flight_issue_ids(self) -> set[str]:
        return self.queue.in_flight_issue_ids()

    def wiki_write_in_flight(self) -> int:
        return self.queue.wiki_write_in_flight()
