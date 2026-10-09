"""issues 线提交入口：失败重试（单发/批量）与 issue_action 派发。

重试先做幂等收敛——该 issue 已有在途行、或资源已被在途行占用时返回既有行，
收敛不到且队列非空才拒（PipelineBusy）。issue_action（rescan）不受互斥约束、
也不计入互斥：它不写 wiki、不产生快照批，可穿插在写批之间执行，结论基于执行中
的 wiki，写批结束后再 rescan 即更新。重试资格在提交口统一判定：编译失败账、
未终态、来源可读。在途判定只由 JobStore.in_flight_kinds 从 jobs 表得出，
无内存状态。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from wiki_agent.issues import (
    IssueActionConflict,
    IssueKind,
    IssueRecord,
    IssueStatus,
    IssueStore,
)
from wiki_agent.issues.retry_source import SourceUnavailableError, resolve_retry_source
from wiki_agent.queue import Job, Kind
from wiki_agent.queue.queue import JobQueue
from wiki_agent.snapshots import SnapshotError, SnapshotStore


class IssueCoordinator:
    """重试与动作的提交口：资格判定、快照捕获、幂等入队。"""

    def __init__(self, *, queue: JobQueue, issues: IssueStore, snapshots: SnapshotStore):
        self.queue = queue
        self.issues = issues
        self.snapshots = snapshots

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

        来源不可读时标记 unavailable（在事务外，避免批事务持写锁时嵌套写库）。
        逐行做执行唯一性收敛：该 issue 已有在途行则返回既有行；资源被在途行占用
        （含本批前序行）则收敛到该行、其无关联 issue 时补挂。两个收敛都不到且
        批外有写 wiki 在途时抛 PipelineBusy——在途计数在事务开始时取定，本批
        自产行不计入。

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
            with self.queue.store.transaction(immediate=True) as conn:
                external_busy = self.queue.in_flight_wiki_write_counts(_conn=conn)
                for issue_id, resource, batch, digest in prepared:
                    existing = self.queue.store.in_flight_job_by_issue(issue_id, _conn=conn)
                    if existing is not None:
                        jobs.append(existing)
                        continue
                    occupant = self.queue.store.in_flight_by_resource(resource, _conn=conn)
                    if occupant is not None:
                        if not occupant.issue_id:
                            occupant = self.queue.store.attach_issue(
                                occupant.id, issue_id, _conn=conn
                            )
                        jobs.append(occupant)
                        continue
                    if external_busy:
                        raise JobQueue.pipeline_busy(external_busy)
                    used.add(batch)
                    jobs.append(
                        self.queue.submit(
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
        return self.queue.submit(
            kind=Kind.ISSUE_ACTION,
            resource=issue_id,
            mode=action,
            payload=payload,
            idempotency_key=f"issue-action:{issue_id}:{action}",
            issue_id=issue_id,
        )
