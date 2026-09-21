"""Job 终态 → Issue 账本 / SyncState 的唯一联动点。

由 JobService.complete_with_outcome 在终态事务内调用 apply(job, result, conn)：
一切跨表写都并入该事务。SyncState 是 JSON 文件、参与不了 SQLite
事务——apply 返回"提交后动作"清单，由 service 在 commit 之后立即执行
（先库后文件：崩溃窗口靠 recover_stale 与 digest 幂等短路收敛，方向
只能是"库里没记成就重做"）。

手动重试模型：失败只做记账——issue 停在 open（attempts 计数、
last_error 快照），不排任何程。人修好环境后再次 sync 即重试；issue 的
retry 按钮走 submit_issue_retry 直投一次性尝试。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from wiki_agent.application.job_results import JobResult
from wiki_agent.issues import IssueDraft, IssueKind, IssueStatus, IssueStore
from wiki_agent.issues.models import JsonObject
from wiki_agent.jobs import Job
from wiki_agent.log import emit_event, get_logger

if TYPE_CHECKING:
    from wiki_agent.sync.state import SyncState

logger = get_logger("JOB_OUTCOMES")


class JobOutcomeHandler:
    """所有 Job 终态副作用规则的单一入口。"""

    def __init__(
        self,
        issue_store: IssueStore,
        *,
        sync_state: SyncState | None = None,
    ):
        self._issues = issue_store
        self._sync_state = sync_state

    # 唯一入口：终态事务内调用

    def apply(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> list[Callable[[], None]]:
        """把 result 的联动写入并入 conn 事务；返回 commit 后要执行的动作。

        分支只覆盖 succeeded/ingest_error/transient——cancelled 刻意无
        联动（无账可还），调用方不必为取消结果走本函数。
        """
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
        elif result.error_type == "ingest_error":
            self._on_ingest_error(job, result, conn)
        elif result.error_type == "transient":
            self._on_transient(job, result, conn)
        # error_type == ""：无联动语义（如未注册 kind），仅留 failed 行
        return post_commit

    # 各分支

    def _on_succeeded(self, job: Job, result: JobResult) -> list[Callable[[], None]]:
        state = self._sync_state
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
        issue = self._issues.report_failure(
            self._draft(job, detail), str(detail.get("error") or ""), _conn=conn
        )
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

    def _on_transient(self, job: Job, result: JobResult, conn: sqlite3.Connection) -> None:
        """未预期异常（bug/环境）→ run_failure 问题入账，同样不排程。"""
        error = str(result.detail.get("error") or "未知异常")[:500]
        draft = IssueDraft(
            kind=IssueKind.RUN_FAILURE,
            title=f"{Path(job.resource).name or job.resource} 执行失败",
            summary=error,
            origin={"mode": job.mode, "reported_by": f"job:{job.kind}", "stage": ""},
            resource={"type": "job", "path": job.resource, "label": job.resource},
            diagnostics={"error": error, "detail": error[:1000]},
            context={"source_path": job.resource},
        )
        self._issues.report_failure(draft, error, _conn=conn)
        logger.error("job %s transient: %s", job.id, error[:200])

    # 账本构造

    def _draft(self, job: Job, detail: dict) -> IssueDraft:
        source = str(detail.get("source") or Path(job.resource).name)
        diagnostics = dict(detail.get("diagnostics") or {})
        diagnostics.setdefault("detail", str(detail.get("error") or "")[:1000])
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
            context={"source_path": str(detail.get("source_path") or job.resource)},
        )
