"""Job 终态 → Issue / SyncState 的唯一联动点。

由 complete_with_outcome 在终态事务内调用 apply(job, result, conn)，所有跨表写
并入该事务。SyncState 是 JSON 文件、进不了 SQLite 事务——apply 返回提交后要执行
的动作清单，由 service 在 commit 后立即执行：先写库再写文件，崩溃时靠 recover_stale
与 digest 幂等重做收敛。

issue 变更只由成功结果的 settlement 决定：handler 申报完成的业务事实类型，本模块
按 ISSUE_RULES 决定动作；没有"成功即一律 resolve"的通用规则，handler 也不直接改
issue。未申报或表中无此类别时不改 issue。

失败只记 issue（停在 open，更新 attempts 与 last_error），不自动排程：环境修好后
再次 sync 即重试，issue 的 retry 按钮走 submit_issue_retry 发起单次尝试。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from wiki_agent.errors import (
    ERROR_DETAIL_LIMIT,
    ERROR_SUMMARY_LIMIT,
    ERROR_TRACE_LIMIT,
    summarize_error,
)
from wiki_agent.issues import (
    InvalidIssueTransitionError,
    IssueAlreadyClaimedError,
    IssueDraft,
    IssueKind,
    IssueStatus,
    IssueStore,
)
from wiki_agent.issues.models import JsonObject
from wiki_agent.jobs import Job, JobResult, Kind, Settlement
from wiki_agent.log import emit_event, get_logger

if TYPE_CHECKING:
    from wiki_agent.sync.state import SyncState

logger = get_logger("JOB_OUTCOMES")


@dataclass(frozen=True, slots=True)
class IssueEffect:
    """一种结算类别对应的 issue 动作。

    resolve_source：resolve 该资源路径上的全部活动失败记录（不止 job 关联的那条，
    同源旧记录一并失效）；transition_linked：只改 job 关联的那条 issue，带 CAS。
    """

    kind: Literal["resolve_source", "transition_linked", "none"]
    target: IssueStatus | None = None
    cause: str = ""


# settlement → issue 动作；未列出的类别不改 issue。新增动作型 job 在此加一行。
ISSUE_RULES: dict[Settlement, IssueEffect] = {
    Settlement.INGESTED: IssueEffect("resolve_source"),
    Settlement.ALREADY_INGESTED: IssueEffect("resolve_source"),
    Settlement.DELETE_APPLIED: IssueEffect("resolve_source", cause="source_deleted"),
    Settlement.RESCAN_STILL_PRESENT: IssueEffect(
        "transition_linked", target=IssueStatus.BLOCKED
    ),
    Settlement.RESCAN_CLEARED: IssueEffect("transition_linked", target=IssueStatus.RESOLVED),
}


class SourcePageWriter(Protocol):
    """compile 成功时写源档案页的端口，由 compiler.extraction 实现。

    经装配根注入，jobs 不直接依赖 compiler。
    """

    def write(self, records_dir: Path, slug: str, content: str) -> None: ...


class JobOutcomeHandler:
    """所有 Job 终态副作用规则的单一入口。"""

    def __init__(
        self,
        issue_store: IssueStore,
        *,
        sync_state: SyncState | None = None,
        source_records_dir: str | Path | None = None,
        source_writer: SourcePageWriter | None = None,
    ):
        self._issues = issue_store
        self._sync_state = sync_state
        self._records_dir = Path(source_records_dir) if source_records_dir is not None else None
        self._source_writer = source_writer

    def apply(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> list[Callable[[], None]]:
        """把 result 的联动写入并入 conn 事务；返回 commit 后要执行的动作。

        只处理 succeeded 与 ingest_error；cancelled 与无联动的失败在此不产生动作。
        """
        post_commit: list[Callable[[], None]] = []
        if result.status == "succeeded":
            post_commit += self._on_succeeded(job, result)
            self._apply_issue_rule(job, result, conn)
        elif result.error_type == "ingest_error":
            self._on_ingest_error(job, result, conn)
        return post_commit

    # settlement → issue 动作（规则表在模块顶部）

    def _apply_issue_rule(self, job: Job, result: JobResult, conn: sqlite3.Connection) -> None:
        raw = str(result.detail.get("settlement") or "")
        if not raw:
            return  # 未申报即无联动语义的成功
        try:
            settlement = Settlement(raw)
        except ValueError:
            logger.warning("job %s 申报了未知结算类别 %r，账本不动", job.id, raw)
            return
        effect = ISSUE_RULES.get(settlement)
        if effect is None or effect.kind == "none":
            return
        if effect.kind == "resolve_source":
            self._resolve_source_failures(job, result, settlement, effect, conn)
        elif job.issue_id:
            self._transition_linked_issue(job, result, settlement, effect, conn)

    def _resolve_source_failures(
        self,
        job: Job,
        result: JobResult,
        settlement: Settlement,
        effect: IssueEffect,
        conn: sqlite3.Connection,
    ) -> None:
        """resolve 该资源路径上的全部活动失败记录（open/blocked）。

        resolution 只记小的可追溯字段；detail 的全文（text/source_page/archive_ops）
        不写进 issue。
        """
        base: JsonObject = {"fixed_by": job.id, "settlement": settlement.value}
        if effect.cause:
            base["cause"] = effect.cause
        for key in ("digest", "commit"):
            value = result.detail.get(key)
            if value:
                base[key] = str(value)
        for record in self._issues.find_pending_failures(job.resource):
            self._issues.transition(
                record.id,
                IssueStatus.RESOLVED,
                resolution=dict(base),
                event="job_succeeded",
                _conn=conn,
            )

    def _transition_linked_issue(
        self,
        job: Job,
        result: JobResult,
        settlement: Settlement,
        effect: IssueEffect,
        conn: sqlite3.Connection,
    ) -> None:
        """只改 job 关联的那条 issue；CAS 带 expected 状态，扫描期间被人工
        改过则保持人工结果。

        当前仅 rescan 使用：executor 只产出复扫结论，终态在此写入。
        """
        assert effect.target is not None
        raw_findings = result.detail.get("rescan_findings")
        resolution: JsonObject = {
            "action": "rescan",
            "still_present": settlement is Settlement.RESCAN_STILL_PRESENT,
            "findings": raw_findings if isinstance(raw_findings, int) else 0,
        }
        try:
            self._issues.transition(
                job.issue_id,
                effect.target,
                resolution=resolution,
                expected={IssueStatus.OPEN, IssueStatus.BLOCKED},
                event="rescan_completed",
                _conn=conn,
            )
        except (InvalidIssueTransitionError, IssueAlreadyClaimedError):
            logger.info(
                "rescan 的 issue %s 已在扫描期间被人工裁决，终态保持人的结论", job.issue_id
            )

    # 各结算分支

    def _on_succeeded(self, job: Job, result: JobResult) -> list[Callable[[], None]]:
        state = self._sync_state
        if state is None:
            return []
        if job.kind == Kind.DELETE:
            # 删除结算（commit 后执行）：应用运行期只规划的档案清理清单 + 移除 state 条目
            ops = result.detail.get("archive_ops")

            def settle_delete() -> None:
                for op in ops if isinstance(ops, list) else []:
                    self._apply_archive_op(op)
                state.drop(job.resource)
                state.save()

            return [settle_delete]
        digest = str(result.detail.get("digest") or "")
        text = result.detail.get("text")
        if job.kind != Kind.COMPILE or not digest or not isinstance(text, str):
            return []
        page = result.detail.get("source_page")

        def settle_compile() -> None:
            if isinstance(page, dict) and page.get("slug") and page.get("content"):
                if self._records_dir is None or self._source_writer is None:
                    logger.warning("缺 source_records_dir/source_writer，档案页未落盘: %s", job.resource)
                else:
                    self._source_writer.write(
                        self._records_dir, str(page["slug"]), str(page["content"])
                    )
            state.record(job.resource, digest, text)

        return [settle_compile]

    @staticmethod
    def _apply_archive_op(op: object) -> None:
        """执行 delete job 规划的档案改写/移除。

        档案页在 wiki 的 git 作用域外、不受回滚保护，故与 issue 变更同点写入。
        """
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
            summarize_error(detail.get("error"), ERROR_TRACE_LIMIT),
        )
        if job.issue_id and issue.id != job.issue_id:
            # 重试 job 的失败合并到了另一条 issue：非预期，留观测
            logger.warning(
                "retry job %s 的失败合并到了新 issue %s（预期 %s）", job.id, issue.id, job.issue_id
            )

    # issue draft 构造

    def _draft(self, job: Job, detail: dict) -> IssueDraft:
        source = str(detail.get("source") or Path(job.resource).name)
        diagnostics = dict(detail.get("diagnostics") or {})
        diagnostics.setdefault("detail", summarize_error(detail.get("error"), ERROR_DETAIL_LIMIT))
        return IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title=f"{source or '来源文件'}处理失败",
            summary=summarize_error(detail.get("error"), ERROR_SUMMARY_LIMIT),
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
