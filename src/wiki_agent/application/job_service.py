"""Unified durable job submission and lifecycle API.

Job 是唯一执行事实来源：提交入口收口在这里，终态写入只有一个点
（complete_with_outcome，I2——jobs 行、transient 链、issue 联动同事务）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wiki_agent.application.job_outcomes import JobOutcomeHandler
from wiki_agent.application.job_results import JobResult
from wiki_agent.config import RetryConfig
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.jobs import Job, JobStore
from wiki_agent.watch.state import WatchState


class JobService:
    """Application boundary for all executable work."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        retry_config: RetryConfig | None = None,
        outcomes: JobOutcomeHandler | None = None,
        watch_state: WatchState | None = None,
    ):
        self.workspace = Path(workspace)
        self.store = JobStore(workspace)
        self.issues = IssueStore(workspace)
        self.issue_service = IssueService(self.issues)
        self.retry_config = retry_config or RetryConfig()
        self.outcomes = outcomes or JobOutcomeHandler(
            self.issues,
            watch_state=watch_state,
            retry_config=self.retry_config,
        )
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
        next_run_at: str = "",
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        return self.store.enqueue(
            kind=kind,
            resource=resource,
            mode=mode,
            payload=payload,
            idempotency_key=idempotency_key,
            issue_id=issue_id,
            next_run_at=next_run_at,
            _conn=_conn,
        )

    def submit_watch_change(self, resource: str, *, deleted: bool = False, digest: str = "") -> Job:
        """提交 watcher 确认的变更；digest 是核账凭证（delete 无内容版本）。"""
        kind = "delete" if deleted else "compile"
        return self.submit(
            kind=kind,
            resource=resource,
            mode="watch",
            payload={"deleted": deleted, "digest": digest},
            idempotency_key=f"watch:{kind}:{resource}",
        )

    def submit_issue_action(
        self, issue_id: str, action: str, payload: dict[str, object] | None = None
    ) -> Job:
        return self.submit(
            kind="issue_action",
            resource=issue_id,
            mode=action,
            payload=payload,
            idempotency_key=f"issue-action:{issue_id}:{action}",
        )

    # 执行生命周期——Worker 独占

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        return self.store.claim_next(kinds=kinds)

    def mark_stage(self, job_id: str, stage: str) -> Job:
        # 同时刷新 updated_at——它是 recover_stale 的存活心跳
        return self.store.update(job_id, stage=stage)

    def complete_with_outcome(self, job: Job, result: JobResult) -> Job:
        """唯一终态提交点：jobs 行、transient 链、issue 联动同事务（I2/I6）。

        返回链式重试的新 job（如有）。WatchState 写文件在事务提交后执行。
        """
        post_commit: list = []
        chain: Job | None = None
        with self.store.database.transaction(immediate=True) as conn:
            error = ""
            if result.status == "succeeded":
                self.store.update(job.id, status="succeeded", stage="completed", _conn=conn)
            elif result.status == "cancelled":
                self.store.update(job.id, status="cancelled", stage="cancelled", _conn=conn)
            else:
                error = str(result.detail.get("error") or "")[:500]
                self.store.update(job.id, status="failed", error=error, _conn=conn)
            # transient 且未耗尽 → 先写终态再排链（同 resource，旧行不占 I1 索引）
            if (
                result.status == "failed"
                and result.error_type == "transient"
                and job.chain_attempt < self.retry_config.source_max_attempts
            ):
                chain = self.store.enqueue(
                    kind=job.kind,
                    resource=job.resource,
                    mode=job.mode,
                    payload={
                        **job.payload,
                        "retry_of": job.id,
                        "attempt_no": job.chain_attempt + 1,
                    },
                    issue_id=job.issue_id,
                    next_run_at=self._transient_backoff(job.chain_attempt),
                    _conn=conn,
                )
                # 链仍在途——本轮 transient 不产生 issue 联动
            else:
                post_commit += self.outcomes.apply(job, result, conn)
        for action in post_commit:
            action()
        return chain if chain is not None else self.store.get(job.id)

    def cancel_terminal(self, job: Job) -> None:
        """进程取消路径：终态 cancelled + 归还所挂 issue，单事务。"""
        with self.store.database.transaction(immediate=True) as conn:
            self.store.update(job.id, status="cancelled", stage="cancelled", _conn=conn)
            self.outcomes.apply(
                job,
                JobResult(status="cancelled", error_type="cancelled"),
                conn,
            )

    def _transient_backoff(self, attempts: int) -> str:
        delay = self.retry_config.source_base_delay_seconds * (2 ** max(0, attempts - 1))
        delay = min(delay, self.retry_config.source_max_delay_seconds)
        return (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()

    # 读取

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.store.list(limit=limit)

    # —— 兼容旧生命周期 API（Worker 之外不再有调用方的历史包袱）——

    def succeed(self, job_id: str) -> Job:
        return self.store.update(job_id, status="succeeded", stage="completed")

    def fail(self, job_id: str, error: str) -> Job:
        return self.store.update(job_id, status="failed", error=error)

    def cancel(self, job_id: str) -> Job:
        return self.store.update(job_id, status="cancelled", stage="cancelled")
