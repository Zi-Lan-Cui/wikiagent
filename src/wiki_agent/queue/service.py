"""业务线提交入口的过渡壳：issue retry 提交口在这里，sync 与维护线委托各自 coordinator。

issue retry 提交的幂等收敛：该 issue 已有在途行、或资源已被在途行占用时返回
既有行，收敛不到且队列非空才拒（PipelineBusy）。issue_action（rescan）不受
互斥约束、也不计入互斥：它不写 wiki、不产生快照批，可穿插在写批之间执行，
结论基于执行中的 wiki，写批结束后再 rescan 即更新。在途判定只由
JobStore.in_flight_kinds 从 jobs 表得出，无内存状态。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from wiki_agent.issues import (
    IssueActionConflict,
    IssueKind,
    IssueRecord,
    IssueStatus,
    IssueStore,
)
from wiki_agent.queue import Job, JobResult, Kind
from wiki_agent.queue.queue import JobQueue
from wiki_agent.queue.retry_source import SourceUnavailableError, resolve_retry_source
from wiki_agent.snapshots import SnapshotError

if TYPE_CHECKING:
    from wiki_agent.maintenance import ProposalStore
    from wiki_agent.maintenance.coordinator import MaintenanceCoordinator
    from wiki_agent.sync.baseline import SyncBaseline
    from wiki_agent.sync.coordinator import SyncCoordinator


class JobService:
    """提交口壳：机制委托 JobQueue，线提交委托 SyncCoordinator / MaintenanceCoordinator。"""

    def __init__(
        self,
        *,
        queue: JobQueue,
        issues: IssueStore,
        sync: SyncCoordinator | None = None,
        maintenance: MaintenanceCoordinator | None = None,
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
        # 基线落后集合的只读入口，供维护流程预检与脚本消费
        self.baseline = baseline
        # 提议账本只读入口：web 处置端点经此查询与否决，入账在 coordinator 事务内
        self.proposals = proposals

    def recover_stale(self) -> int:
        return self.queue.recover_stale()

    @property
    def recovered_jobs(self) -> int:
        return self.queue.recovered_jobs

    def _in_flight_wiki_write_counts(
        self, _conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        return self.queue.in_flight_wiki_write_counts(_conn=_conn)

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

    def submit(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        issue_id: str = "",
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        return self.queue.submit(
            kind=kind,
            resource=resource,
            mode=mode,
            payload=payload,
            idempotency_key=idempotency_key,
            issue_id=issue_id,
            _conn=_conn,
        )

    @staticmethod
    def _raise_if_retry_ineligible(issue: IssueRecord) -> None:
        """重试资格在提交口统一判定：编译失败记录且未终态。

        来源可读性在批内逐行判（resolve_retry_source）；标记 unavailable 的入账
        由调用方处理，提交口只拒绝、不改 issue 状态。
        """
        if issue.kind != IssueKind.INGESTION_FAILURE:
            raise IssueActionConflict(f"不是编译失败账，不能重试: {issue.kind}")
        if issue.status in {IssueStatus.RESOLVED, IssueStatus.DISMISSED}:
            raise IssueActionConflict("问题已终态，先重新打开再重试")

    def submit_issue_retry(self, issue_id: str) -> Job:
        """单发重试即一批——资格、收敛、互斥的语义见 submit_issue_retry_batch。"""
        return self.submit_issue_retry_batch([issue_id])[0]

    def submit_issue_retry_batch(self, issue_ids: list[str]) -> list[Job]:
        """批量重试：资格与输入快照在事务前，收敛与入队在单事务内。

        资格在提交口统一判定：编译失败记录、未终态、来源可读；来源不可读时
        标记 unavailable（在事务外，避免批事务持写锁时嵌套写库）。逐行做执行
        唯一性收敛：该 issue 已有在途行则返回既有行；资源被在途行占用（含本批
        前序行）则收敛到该行、其无关联 issue 时补挂。两个收敛都不到且批外有写
        wiki 在途时抛 PipelineBusy——在途计数在事务开始时取定，本批自产行不计入。

        快照语义同 submit_sync：digest 取自点击时复制的副本。任一行资格不过、
        输入不可读或遇互斥，整批回滚并删除已复制的快照目录。issue 终态由
        compile job 的 outcome 落，提交不改 issue 状态。
        """
        prepared: list[tuple[str, str, str, str]] = []
        fresh_batches: list[str] = []
        try:
            for issue_id in issue_ids:
                issue = self.issues.require(issue_id)
                self._raise_if_retry_ineligible(issue)
                unavailable = str(issue.retry.get("unavailable_reason") or "")
                if unavailable:
                    raise SourceUnavailableError(unavailable)
                try:
                    resource = str(Path(resolve_retry_source(issue)).resolve())
                    batch = f"retry_{uuid4().hex}"
                    fresh_batches.append(batch)
                    digest = self.snapshots.capture(batch, Path(resource).parent, [resource])[
                        resource
                    ]
                except (SourceUnavailableError, SnapshotError) as exc:
                    reason = str(exc)
                    self.mark_retry_unavailable(issue_id, reason)
                    raise SourceUnavailableError(reason) from exc
                prepared.append((issue_id, resource, batch, digest))
            jobs: list[Job] = []
            used: set[str] = set()
            with self.store.transaction(immediate=True) as conn:
                external_busy = self._in_flight_wiki_write_counts(_conn=conn)
                for issue_id, resource, batch, digest in prepared:
                    existing = self.store.in_flight_job_by_issue(issue_id, _conn=conn)
                    if existing is not None:
                        jobs.append(existing)
                        continue
                    occupant = self.store.in_flight_by_resource(resource, _conn=conn)
                    if occupant is not None:
                        if not occupant.issue_id:
                            occupant = self.store.attach_issue(occupant.id, issue_id, _conn=conn)
                        jobs.append(occupant)
                        continue
                    if external_busy:
                        raise JobQueue.pipeline_busy(external_busy)
                    used.add(batch)
                    jobs.append(
                        self.store.enqueue(
                            kind=Kind.COMPILE,
                            resource=resource,
                            mode="issue_retry",
                            payload={
                                "deleted": False,
                                "digest": digest,
                                "batch": batch,
                                "rel_path": Path(resource).name,
                            },
                            idempotency_key=f"issue-retry:{issue_id}",
                            issue_id=issue_id,
                            _conn=conn,
                        )
                    )
        except BaseException:
            for batch in fresh_batches:
                self.snapshots.drop_batch(batch)
            raise
        for batch in fresh_batches:
            if batch not in used:
                self.snapshots.drop_batch(batch)
        return jobs

    def mark_retry_unavailable(self, issue_id: str, reason: str) -> None:
        """标记该 issue 输入不可读：写 retry.unavailable_reason，OPEN 转 BLOCKED。"""
        record = self.issues.require(issue_id)
        self.issues.update_payloads(
            issue_id,
            retry={**record.retry, "unavailable_reason": reason},
            diagnostics={**record.diagnostics, "detail": reason},
            event="retry_source_unavailable",
        )
        if record.status == IssueStatus.OPEN:
            self.issues.transition(
                issue_id, IssueStatus.BLOCKED, event="blocked_source_unavailable"
            )

    def submit_issue_action(
        self, issue_id: str, action: str, payload: dict[str, object] | None = None
    ) -> Job:
        return self.submit(
            kind=Kind.ISSUE_ACTION,
            resource=issue_id,
            mode=action,
            payload=payload,
            idempotency_key=f"issue-action:{issue_id}:{action}",
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
