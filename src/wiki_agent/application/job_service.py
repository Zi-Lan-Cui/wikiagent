"""Unified durable job submission and lifecycle API.

Job 是唯一执行事实来源：提交入口收口在这里，终态写入只有一个点
（complete_with_outcome——jobs 行与 issue 联动同事务、带 CAS）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from wiki_agent.application.job_outcomes import JobOutcomeHandler
from wiki_agent.application.job_results import JobResult
from wiki_agent.compiler.workflows.retry import SourceUnavailableError, resolve_retry_source
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.jobs import DuplicateInFlightJob, Job, JobStore, SyncInProgress
from wiki_agent.sync.state import SyncState, digest_file_text, scan_disk


class JobService:
    """Application boundary for all executable work."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        outcomes: JobOutcomeHandler | None = None,
        sync_state: SyncState | None = None,
        wiki_dir: str | Path | None = None,
    ):
        self.workspace = Path(workspace)
        # issue retry 解析重试输入需要 wiki 根（wiki_page 型资源的定位）
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else None
        # sync 快照对比需要完成账本
        self.sync_state = sync_state
        self.store = JobStore(workspace)
        self.issues = IssueStore(workspace)
        self.issue_service = IssueService(self.issues)
        self.outcomes = outcomes or JobOutcomeHandler(self.issues, sync_state=sync_state)
        self.recovered_jobs = self.store.recover_stale()

    # 提交

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
        return self.store.enqueue(
            kind=kind,
            resource=resource,
            mode=mode,
            payload=payload,
            idempotency_key=idempotency_key,
            issue_id=issue_id,
            _conn=_conn,
        )

    def submit_issue_retry(self, issue_id: str) -> Job:
        """重试请求 → compile Job：提交点三重防线收敛为"至多一个在途、返回既有"。

        ① 挂账预查（该 issue 已有在途 job 直接返回）；② 幂等键命中在途行
        返回既有；③ 撞唯一在途（他人占位）返回占位者、无账则补挂。retry 资格
        （状态、来源可读）由各入口的 validate 判定，这里只管执行
        唯一性。issue 终态由 compile job 的 outcome 落（成功 RESOLVED /
        再失败记一笔账等人），提交本身不改变 issue 状态。
        """
        if self.wiki_dir is None:
            raise RuntimeError("submit_issue_retry 需要 wiki_dir")
        issue = self.issues.require(issue_id)
        source = resolve_retry_source(issue, self.wiki_dir)
        resource = str(Path(source).resolve())
        read = digest_file_text(resource)
        if read is None:
            raise SourceUnavailableError(f"重试输入不可读: {resource}")
        with self.store.database.transaction(immediate=True) as conn:
            existing = self.store.in_flight_job_by_issue(issue_id, _conn=conn)
            if existing is not None:
                return existing
            try:
                return self.store.enqueue(
                    kind="compile",
                    resource=resource,
                    mode="issue_retry",
                    payload={"deleted": False, "digest": read[0]},
                    idempotency_key=f"issue-retry:{issue_id}",
                    issue_id=issue_id,
                    _conn=conn,
                )
            except DuplicateInFlightJob:
                occupant = self.store.in_flight_by_resource(resource, _conn=conn)
                if occupant is None:  # 撞唯一索引必有占位者——防御性外抛
                    raise
                if not occupant.issue_id:
                    occupant = self.store.attach_issue(occupant.id, issue_id, _conn=conn)
                return occupant

    def submit_issue_action(
        self, issue_id: str, action: str, payload: dict[str, object] | None = None
    ) -> Job:
        return self.submit(
            kind="issue_action",
            resource=issue_id,
            mode=action,
            payload=payload,
            idempotency_key=f"issue-action:{issue_id}:{action}",
            issue_id=issue_id,
        )

    # sync 快照

    def sync_status(self, source_dir: str | Path) -> dict[str, int]:
        """只读快照预演：dirty/removed 计数与在途闸状态——不提交任何东西。"""
        if self.sync_state is None:
            raise RuntimeError("sync_status 需要 sync_state")
        disk = scan_disk(source_dir)
        dirty, removed = self.sync_state.diff(disk)
        return {
            "dirty": len(dirty),
            "removed": len(removed),
            "in_flight": self.store.in_flight_for_kinds(("compile", "delete")),
        }

    def submit_sync(self, source_dir: str | Path) -> list[Job]:
        """快照同步：本事务内的"磁盘 − 账本"之差即本次批次，整批入队。

        语义契约：sync 互斥串行（compile/delete 有在途则 SyncInProgress），
        执行中的新改动属于下一次快照——"账本落后一个版本"是合法状态而非
        事故，因此执行体无需凭证校验，失败不写账即保持脏、再次 sync 即重试。
        脏文件若背着 open 失败账则顺手挂账 issue_id（成功即销账）。
        """
        if self.sync_state is None:
            raise RuntimeError("submit_sync 需要 sync_state")
        disk = scan_disk(source_dir)
        with self.store.database.transaction(immediate=True) as conn:
            if self.store.in_flight_for_kinds(("compile", "delete"), _conn=conn) > 0:
                raise SyncInProgress()
            dirty, removed = self.sync_state.diff(disk)
            batch = f"sync_{uuid4().hex}"
            jobs: list[Job] = []
            for path, digest in dirty:
                pending = self.issues.find_pending_failures(path)
                jobs.append(
                    self.store.enqueue(
                        kind="compile",
                        resource=path,
                        mode="sync",
                        payload={"deleted": False, "digest": digest},
                        idempotency_key=f"{batch}:{path}",
                        issue_id=pending[0].id if pending else "",
                        _conn=conn,
                    )
                )
            for path in removed:
                jobs.append(
                    self.store.enqueue(
                        kind="delete",
                        resource=path,
                        mode="sync",
                        payload={"deleted": True, "digest": ""},
                        idempotency_key=f"{batch}:{path}",
                        _conn=conn,
                    )
                )
        return jobs

    # 执行生命周期——Worker 独占

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        return self.store.claim_next(kinds=kinds)

    def mark_stage(self, job_id: str, stage: str) -> Job:
        # 同时刷新 updated_at——它是 recover_stale 的存活心跳
        return self.store.update(job_id, stage=stage)

    def complete_with_outcome(self, job: Job, result: JobResult) -> Job:
        """唯一终态提交点：jobs 行与 issue 联动同事务。

        终态写入带 CAS（仅 running 可翻转）：行已被取代/取消时迟到写静默
        跳过——不联动、不记账，返回行的现状。SyncState 写文件在提交后执行。
        手动重试模型下这里不产生任何后继 job：失败就是终态 + 一笔账。
        """
        with self.store.database.transaction(immediate=True) as conn:
            won = self.store.try_finalize(
                job.id,
                status=result.status,
                stage={"succeeded": "completed", "cancelled": "cancelled"}.get(result.status),
                error=(
                    str(result.detail.get("error") or "")[:500]
                    if result.status == "failed"
                    else None
                ),
                _conn=conn,
            )
            if not won:
                return self.store.get(job.id, _conn=conn)
            post_commit = self.outcomes.apply(job, result, conn)
        for action in post_commit:
            action()
        return self.store.get(job.id)

    def cancel_terminal(self, job: Job) -> None:
        """进程取消路径：终态 cancelled（CAS，不二次写）。

        取消不背失败也不还账——提交从未改变 issue 状态，无账可还，
        因此无需任何 issue/state 联动，一次条件写就是全部工作。
        """
        self.store.try_finalize(job.id, status="cancelled", stage="cancelled")

    # 读取

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.store.list(limit=limit)
