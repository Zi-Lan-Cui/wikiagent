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
from typing import TYPE_CHECKING

from wiki_agent.compiler.extraction import write_source_page
from wiki_agent.compiler.models import SourcePage
from wiki_agent.issues import (
    InvalidIssueTransitionError,
    IssueAlreadyClaimedError,
    IssueDraft,
    IssueKind,
    IssueStatus,
    IssueStore,
)
from wiki_agent.issues.models import JsonObject
from wiki_agent.jobs import Job, JobResult
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
        source_records_dir: str | Path | None = None,
    ):
        self._issues = issue_store
        self._sync_state = sync_state
        self._records_dir = Path(source_records_dir) if source_records_dir is not None else None

    # 唯一入口：终态事务内调用

    def apply(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> list[Callable[[], None]]:
        """把 result 的联动写入并入 conn 事务；返回 commit 后要执行的动作。

        分支只覆盖 succeeded/ingest_error——cancelled 与无联动语义的失败
        （handler bug 由 Worker 记日志+事件承接）在此都是 no-op。
        """
        post_commit: list[Callable[[], None]] = []
        if result.status == "succeeded":
            post_commit += self._on_succeeded(job, result)
            if job.kind == "issue_action" and job.mode == "rescan":
                # rescan 的终态按其复扫结论裁决（见 _settle_rescan），
                # 不适用下面的"成功即销账"
                self._settle_rescan(job, result, conn)
            elif job.issue_id:
                # source 材料处理成功 = 销账。resolution 只留小的可追溯字段
                # ——detail 里的全文（text/source_page/archive_ops）进账本纯属冗余
                resolution: JsonObject = {"fixed_by": job.id}
                for key in ("digest", "commit"):
                    value = result.detail.get(key)
                    if value:
                        resolution[key] = str(value)
                self._issues.transition(
                    job.issue_id,
                    IssueStatus.RESOLVED,
                    resolution=resolution,
                    event="job_succeeded",
                    _conn=conn,
                )
        elif result.error_type == "ingest_error":
            self._on_ingest_error(job, result, conn)
        # 其余（cancelled、handler bug 的无联动 failed）刻意零动作
        return post_commit

    def _settle_rescan(self, job: Job, result: JobResult, conn: sqlite3.Connection) -> None:
        """rescan 的终局裁决与 job 终态同事务写入（issue 状态唯一写入点）。

        executor 只产出依据（rescan_still_present）：仍在→BLOCKED（确认过的
        待处理）、消失→RESOLVED。CAS expected 挡住竞态——扫描期间被人工
        裁决掉的 issue 保持人的结论，此时扫描本身已完成、job 仍记 succeeded。
        """
        still_present = bool(result.detail.get("rescan_still_present"))
        raw_findings = result.detail.get("rescan_findings")
        resolution: JsonObject = {
            "action": "rescan",
            "still_present": still_present,
            "findings": raw_findings if isinstance(raw_findings, int) else 0,
        }
        try:
            self._issues.transition(
                job.issue_id,
                IssueStatus.BLOCKED if still_present else IssueStatus.RESOLVED,
                resolution=resolution,
                expected={IssueStatus.OPEN, IssueStatus.BLOCKED},
                event="rescan_completed",
                _conn=conn,
            )
        except (InvalidIssueTransitionError, IssueAlreadyClaimedError):
            logger.info(
                "rescan 的 issue %s 已在扫描期间被人工裁决，终态保持人的结论", job.issue_id
            )

    # 各分支

    def _on_succeeded(self, job: Job, result: JobResult) -> list[Callable[[], None]]:
        state = self._sync_state
        if state is None:
            return []
        if job.kind == "delete":
            # 删除结算（延迟到 commit 后，先库后文件）：溯源档案清理清单
            # （消费者执行中只规划不落盘）+ state 条目移除，一并落盘。

            ops = result.detail.get("archive_ops")

            def settle_delete() -> None:
                for op in ops if isinstance(ops, list) else []:
                    self._apply_archive_op(op)
                state.drop(job.resource)
                state.save()

            return [settle_delete]
        digest = str(result.detail.get("digest") or "")
        text = result.detail.get("text")
        if job.kind != "compile" or not digest or not isinstance(text, str):
            return []
        page = result.detail.get("source_page")

        def settle_compile() -> None:
            if isinstance(page, dict) and page.get("slug") and page.get("content"):
                if self._records_dir is None:
                    logger.warning("缺 source_records_dir，档案页未落盘: %s", job.resource)
                else:
                    write_source_page(
                        self._records_dir,
                        SourcePage(slug=str(page["slug"]), content=str(page["content"])),
                    )
            state.record(job.resource, digest, text)

        return [settle_compile]

    @staticmethod
    def _apply_archive_op(op: object) -> None:
        """执行 delete job 规划的溯源档案改写/移除——档案页在 scope 外，
        不受 wiki 的 git 回滚保护，因此与账本同点结算。"""
        if not isinstance(op, dict):
            return
        target = Path(str(op.get("path") or ""))
        if op.get("action") == "unlink":
            target.unlink(missing_ok=True)
        elif op.get("action") == "rewrite" and target.is_file():
            target.write_text(str(op.get("content") or ""), encoding="utf-8")

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
