"""Job 终态 → Issue 账本 / WatchState 的唯一联动点。

由 JobService.complete_with_outcome 在终态事务内调用 apply(job, result, conn)：
一切跨表写都并入该事务。WatchState 是 JSON 文件、参与不了 SQLite
事务——apply 返回"提交后动作"清单，由 service 在 commit 之后立即执行
（先库后文件：崩溃窗口靠对账与 digest 幂等短路收敛，方向只能是
"库里没记成就重做"）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from wiki_agent.application.job_results import JobResult
from wiki_agent.compiler.workflows.failures import (
    retry_attempt_count,
    source_backoff_seconds,
    source_retry_decision,
)
from wiki_agent.config import RetryConfig
from wiki_agent.issues import IssueDraft, IssueKind, IssueStatus, IssueStore
from wiki_agent.issues.models import JsonObject
from wiki_agent.jobs import Job
from wiki_agent.log import emit_event, get_logger

if TYPE_CHECKING:
    from wiki_agent.watch.state import WatchState

logger = get_logger("JOB_OUTCOMES")

_RETRY_WINDOW_HOURS = 24


class JobOutcomeHandler:
    """所有 Job 终态副作用规则的单一入口。"""

    def __init__(
        self,
        issue_store: IssueStore,
        *,
        watch_state: WatchState | None = None,
        retry_config: RetryConfig | None = None,
    ):
        self._issues = issue_store
        self._watch_state = watch_state
        self._retry = retry_config or RetryConfig()

    # 唯一入口：终态事务内调用

    def apply(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> list[Callable[[], None]]:
        """把 result 的联动写入并入 conn 事务；返回 commit 后要执行的动作。"""
        post_commit: list[Callable[[], None]] = []
        if result.status == "succeeded":
            post_commit += self._on_succeeded(job, result)
            if job.issue_id:
                # detail 值类型宽于 JsonValue——持久化时统一 json.dumps，cast 安全
                resolution = cast(JsonObject, {"fixed_by": job.id, **result.detail})
                self._issues.transition(
                    job.issue_id,
                    IssueStatus.RESOLVED,
                    resolution=resolution,
                    event="job_succeeded",
                    _conn=conn,
                )
        # cancelled 无联动：提交不改变 issue 状态，取消即无账可还
        elif result.error_type == "ingest_error":
            self._on_ingest_error(job, result, conn)
        elif result.error_type == "transient":
            self._on_transient(job, result, conn)
        # error_type == ""：无联动语义（如未注册 kind），仅留 failed 行
        return post_commit

    def transient_is_terminal(self, job: Job) -> bool:
        """transient 失败是否已耗尽链式重试（链代数见 Job.chain_attempt）。"""
        return job.chain_attempt >= self._retry.source_max_attempts

    # 各分支

    def _on_succeeded(self, job: Job, result: JobResult) -> list[Callable[[], None]]:
        state = self._watch_state
        if state is None:
            return []
        if job.kind == "delete":
            # 删除确认落账：state 条目由消费者清掉（延迟到 commit 后，先库后文件）

            def drop() -> None:
                state.drop(job.resource)
                state.save()

            return [drop]
        digest = str(result.detail.get("digest") or "")
        text = result.detail.get("text")
        if job.kind != "compile" or not digest or not isinstance(text, str):
            return []

        def record() -> None:
            state.record(job.resource, digest, text)

        return [record]

    def _on_ingest_error(self, job: Job, result: JobResult, conn: sqlite3.Connection) -> None:
        detail = result.detail
        retry = self._initial_schedule()
        issue = self._issues.report(self._draft(job, detail, retry), _conn=conn)
        emit_event(
            "ingest_failure",
            issue_id=issue.id,
            mode=str(detail.get("mode") or job.mode),
            file=str(detail.get("source") or job.resource),
            stage=str(detail.get("stage") or ""),
            error=str(detail.get("error") or ""),
            raw=str(detail.get("raw") or ""),
        )
        logger.error(
            "job %s ingest_error [%s] %s",
            job.id,
            detail.get("stage"),
            str(detail.get("error"))[:200],
        )
        if job.issue_id and issue.id != job.issue_id:
            # 重试 job 撞上了他人合并出的不同指纹——理论上不该发生，留观测
            logger.warning(
                "retry job %s 的失败合并到了新 issue %s（预期 %s）", job.id, issue.id, job.issue_id
            )
        if job.issue_id:
            # 这是 issue 重试链上的一环：推进该 issue 的退避/耗尽判定
            self._advance_issue_schedule(job, result, conn)

    def _advance_issue_schedule(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> None:
        """issue 重试链上的一次失败——推进退避，耗尽转 BLOCKED 归人裁决。"""
        issue = self._issues.get(job.issue_id, _conn=conn)
        if issue is None:
            return
        attempts = retry_attempt_count(issue.retry) + 1
        retry: JsonObject = {
            **issue.retry,
            "attempts": attempts,
            "last_error": str(result.detail.get("error") or "")[:500],
        }
        retryable = (
            source_retry_decision(retry, max_attempts=self._retry.source_max_attempts) == "retry"
        )
        retry["next_retry_at"] = (
            (
                datetime.now(UTC)
                + timedelta(
                    seconds=source_backoff_seconds(
                        attempts,
                        base=self._retry.source_base_delay_seconds,
                        max_delay=self._retry.source_max_delay_seconds,
                    )
                )
            ).isoformat()
            if retryable
            else ""
        )
        # update_payloads 整列替换——先并入 report 刚合并的结构化诊断
        self._issues.update_payloads(
            job.issue_id,
            retry=retry,
            diagnostics={**issue.diagnostics, "detail": retry["last_error"]},
            event="source_retry_failed",
            _conn=conn,
        )
        # 归还可调度状态：未耗尽回 open 等下一轮退避，耗尽转 blocked 归人
        self._issues.transition(
            job.issue_id,
            IssueStatus.OPEN if retryable else IssueStatus.BLOCKED,
            event="retry_requires_decision" if not retryable else "retry_backoff",
            _conn=conn,
        )

    def _on_transient(self, job: Job, result: JobResult, conn: sqlite3.Connection) -> None:
        if job.chain_attempt < self._retry.source_max_attempts:
            # 链式重试 job 由 service 在事务内排入，这里不产生 issue 噪音
            return
        error = str(result.detail.get("error") or "未知异常")[:500]
        self._issues.report(
            IssueDraft(
                kind=IssueKind.RUN_FAILURE,
                title=f"{Path(job.resource).name or job.resource} 执行失败（重试耗尽）",
                summary=error,
                origin={"mode": job.mode, "reported_by": f"job:{job.kind}", "stage": ""},
                resource={"type": "job", "path": job.resource, "label": job.resource},
                diagnostics={"error": error, "attempts": job.chain_attempt},
                retry={"policy": "manual", "attempts": job.chain_attempt, "next_retry_at": ""},
                context={"source_path": job.resource},
            ),
            _conn=conn,
        )
        logger.error("job %s transient 重试耗尽: %s", job.id, error[:200])

    # draft 与退避构造

    def _initial_schedule(self) -> dict:
        """新报失败的初始 retry 计划——首档退避后由 scheduler 接棒。"""
        return {
            "policy": "auto_retry",
            "attempts": 1,
            "next_retry_at": (
                datetime.now(UTC) + timedelta(seconds=self._retry.source_base_delay_seconds)
            ).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(hours=_RETRY_WINDOW_HOURS)).isoformat(),
        }

    def _draft(self, job: Job, detail: dict, retry: dict) -> IssueDraft:
        source = str(detail.get("source") or Path(job.resource).name)
        diagnostics = dict(detail.get("diagnostics") or {})
        diagnostics.setdefault("detail", str(detail.get("error") or "")[:1000])
        retry = {**retry, "policy": str(detail.get("retry_policy") or "auto_retry")}
        return IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title=f"{source or '来源文件'}处理失败",
            summary=str(detail.get("error") or "")[:500],
            origin={
                "mode": str(detail.get("mode") or job.mode),
                "reported_by": f"job:{job.kind}",
                "stage": str(detail.get("stage") or ""),
            },
            resource={
                "type": str(detail.get("source_kind") or "input_file"),
                "path": source,
                "label": source,
            },
            diagnostics=diagnostics,
            retry=retry,
            context={"source_path": str(detail.get("source_path") or job.resource)},
        )
