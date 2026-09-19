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
from wiki_agent.compiler.workflows.retry import SourceUnavailableError, resolve_retry_source
from wiki_agent.config import RetryConfig
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.models import IssueStatus
from wiki_agent.jobs import DuplicateActiveJob, Job, JobStore
from wiki_agent.watch.state import WatchState, digest_file_text


class JobService:
    """Application boundary for all executable work."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        retry_config: RetryConfig | None = None,
        outcomes: JobOutcomeHandler | None = None,
        watch_state: WatchState | None = None,
        wiki_dir: str | Path | None = None,
    ):
        self.workspace = Path(workspace)
        # issue retry 解析重试输入需要 wiki 根（wiki_page 型资源的定位）
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else None
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

    def submit_watch_change(
        self, resource: str, *, deleted: bool = False, digest: str = ""
    ) -> Job | None:
        """提交 watcher 确认的变更；返回 None = 被在途任务合并/让位 issue。

        compile：撞在途 → 排队行合并 digest（意图前进）、执行中行吞掉
        （成功后账会被 pre/post 拒掉，下一轮回退扫描重提交）；存在未到期
        的 auto_retry 失败账 → 让位 issue 通道（I6 唯一重试通道）。
        delete：撞任何在途 → 取代（cancel 让位 + 同事务归还其 issue）。
        """
        kind = "delete" if deleted else "compile"
        key = f"watch:{kind}:{resource}"
        payload: dict[str, object] = {"deleted": deleted, "digest": digest}
        if deleted:
            if self.store.active_by_resource(resource) is not None:
                return self.submit_replace(
                    kind="delete",
                    resource=resource,
                    mode="watch",
                    payload=payload,
                    idempotency_key=key,
                )
            return self._submit_watch_row(kind, resource, payload, key, issue_id="")
        defer, due_issue = self._watch_deferral(resource)
        if defer:
            return None
        result = self._submit_watch_row(kind, resource, payload, key, issue_id=due_issue)
        if result is None:
            # 撞 I1（被其他 kind 占位）——只有排队中的普通 watch compile 行值得合并
            active = self.store.active_by_resource(resource)
            if (
                active is not None
                and active.kind == "compile"
                and active.status == "queued"
                and digest
                and not active.payload.get("retry_of")
            ):
                return self.store.coalesce_payload(active.id, payload)
            return None
        if str(result.payload.get("digest")) == digest:
            return result
        if digest and result.status == "queued" and not result.payload.get("retry_of"):
            # 幂等命中排队旧行——digest 落后即意图前进，合并避免空转；
            # 链式重试行（带 retry_of 谱系）不合并，让它跑完自己的载荷
            return self.store.coalesce_payload(result.id, payload)
        # 命中执行中行：不新建也不合并——成功账会被 pre/post 拒掉，
        # 下一轮回退扫描以新内容重提交（对账兜底）
        return None

    def _submit_watch_row(
        self, kind: str, resource: str, payload: dict, key: str, *, issue_id: str
    ) -> Job | None:
        try:
            return self.submit(
                kind=kind,
                resource=resource,
                mode="watch",
                payload=payload,
                idempotency_key=key,
                issue_id=issue_id,
            )
        except DuplicateActiveJob:
            return None

    def _watch_deferral(self, resource: str) -> tuple[bool, str]:
        """(是否让位, 到期可续链的 issue_id)。只认 auto_retry/retry_once 策略。

        manual 失败账不挡新内容——用户改了文件就是新信号，重蹈失败会按
        指纹合并进同一 issue（occurrences 可见）。
        """
        now = datetime.now(UTC).isoformat()
        for issue in self.issues.find_pending_failures(resource):
            policy = str(issue.retry.get("policy") or "")
            if policy not in {"auto_retry", "retry_once"}:
                continue
            next_retry_at = str(issue.retry.get("next_retry_at") or "")
            if next_retry_at and next_retry_at > now:
                return True, ""
            return False, issue.id
        return False, ""

    def submit_replace(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object],
        idempotency_key: str | None = None,
    ) -> Job:
        """取代在途任务：同事务 cancel 旧行（含归还其 issue）+ 排新行。"""
        with self.store.database.transaction(immediate=True) as conn:
            active = self.store.active_by_resource(resource, _conn=conn)
            if active is not None:
                self.store.update(active.id, status="cancelled", stage="cancelled", _conn=conn)
                self.outcomes.apply(
                    active, JobResult(status="cancelled", error_type="cancelled"), conn
                )
            return self.store.enqueue(
                kind=kind,
                resource=resource,
                mode=mode,
                payload=payload,
                idempotency_key=idempotency_key,
                _conn=conn,
            )

    def submit_issue_retry(self, issue_id: str) -> Job:
        """重试请求 → compile Job：claim + enqueue + PROCESSING 单事务（I3）。

        issue 终态由 compile job 的 outcome 落（成功 RESOLVED / 再失败推进
        退避）——这里只负责"把请求变成在途任务"，双击由 claim CAS 与
        I1 唯一索引共同挡下。
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
            action_id = self.issues.claim_action(
                issue_id, "retry", {"resource": resource}, _conn=conn
            )
            try:
                job = self.store.enqueue(
                    kind="compile",
                    resource=resource,
                    mode="issue_retry",
                    payload={"deleted": False, "digest": read[0]},
                    idempotency_key=f"issue-retry:{issue_id}",
                    issue_id=issue_id,
                    _conn=conn,
                )
            except DuplicateActiveJob as exc:
                self.issues.fail_action(action_id, "该资源已有在途任务", _conn=conn)
                raise exc
            # action 账本同事务收口为"已委托"——执行与终态归 compile job
            self.issues.complete_action(
                action_id,
                status=IssueStatus.PROCESSING,
                result={"delegated_job_id": job.id},
                _conn=conn,
            )
        return job

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
