"""Unified durable job submission and lifecycle API.

Job 是唯一执行事实来源：提交入口收口在这里，终态写入只有一个点
（complete_with_outcome——jobs 行、transient 链、issue 联动同事务）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from wiki_agent.application.job_outcomes import JobOutcomeHandler
from wiki_agent.application.job_results import JobResult
from wiki_agent.compiler.workflows.retry import SourceUnavailableError, resolve_retry_source
from wiki_agent.config import RetryConfig
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.jobs import DuplicateInFlightJob, Job, JobStore, SyncInProgress
from wiki_agent.watch.state import WatchState, digest_file_text, scan_disk


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
        # sync 快照对比需要完成账本
        self.watch_state = watch_state
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
        的 auto_retry 失败账 → 让位 issue 通道（每类失败唯一重试通道）。
        delete：撞任何在途 → 取代（同事务 cancel 让位；取消无账可还）。
        """
        kind = "delete" if deleted else "compile"
        key = f"watch:{kind}:{resource}"
        payload: dict[str, object] = {"deleted": deleted, "digest": digest}
        if deleted:
            if self.store.in_flight_by_resource(resource) is not None:
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
            # 撞唯一在途（被其他 kind 占位）——只有排队中的普通 watch compile 行值得合并
            active = self.store.in_flight_by_resource(resource)
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
        except DuplicateInFlightJob:
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
        """取代在途任务：同事务 cancel 旧行（cancelled 无联动）+ 排新行。"""
        with self.store.database.transaction(immediate=True) as conn:
            active = self.store.in_flight_by_resource(resource, _conn=conn)
            if active is not None:
                self.store.update(active.id, status="cancelled", stage="cancelled", _conn=conn)
            return self.store.enqueue(
                kind=kind,
                resource=resource,
                mode=mode,
                payload=payload,
                idempotency_key=idempotency_key,
                _conn=conn,
            )

    def submit_issue_retry(self, issue_id: str) -> Job:
        """重试请求 → compile Job：提交点三重防线收敛为"至多一个在途、返回既有"。

        ① 挂账预查（该 issue 已有在途 job 直接返回）；② 幂等键命中在途行
        返回既有；③ 撞唯一在途（他人占位）返回占位者、无账则补挂。retry 资格
        （状态、来源可读）由各入口的 validate/调度过滤判定，这里只管执行
        唯一性。issue 终态由 compile job 的 outcome 落（成功 RESOLVED /
        再失败推进退避），提交本身不改变 issue 状态。
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
        if self.watch_state is None:
            raise RuntimeError("sync_status 需要 watch_state")
        disk = scan_disk(source_dir)
        dirty, removed = self.watch_state.diff(disk)
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
        if self.watch_state is None:
            raise RuntimeError("submit_sync 需要 watch_state")
        disk = scan_disk(source_dir)
        with self.store.database.transaction(immediate=True) as conn:
            if self.store.in_flight_for_kinds(("compile", "delete"), _conn=conn) > 0:
                raise SyncInProgress()
            dirty, removed = self.watch_state.diff(disk)
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
        """唯一终态提交点：jobs 行、transient 链、issue 联动同事务。

        终态写入带 CAS（仅 running 可翻转）：行已被取代/取消时迟到写静默
        跳过——不排链、不联动、不记账，返回行的现状。
        返回链式重试的新 job（如有）。WatchState 写文件在事务提交后执行。
        """
        post_commit: list = []
        chain: Job | None = None
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
            # transient 且未耗尽 → 先写终态再排链（同 resource，旧行已不占唯一索引）
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
        """进程取消路径：终态 cancelled（CAS，不二次写）。

        取消不背失败也不还账——提交从未改变 issue 状态，无账可还，
        因此无需任何 issue/state 联动，一次条件写就是全部工作。
        """
        self.store.try_finalize(job.id, status="cancelled", stage="cancelled")

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
