"""issue 线的终态联动规则：按结算类别流转账本，ingest 失败上报与合并。

settlement → 动作表在这里：resolve_source 解决该源全部活动失败记录（不止
job 关联的那条，同源旧记录一并失效）；transition_linked 只改 job 关联的
一条、带 CAS。失败只记 issue（停在 open，更新 attempts 与 last_error），
不自动排程：环境修好后再次 sync 即重试，issue 的 retry 按钮走
submit_issue_retry 发起单次尝试。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
from wiki_agent.log import emit_event, get_logger
from wiki_agent.queue import Job, JobResult, Settlement
from wiki_agent.queue.outcomes import JobOutcomeHandler

logger = get_logger("ISSUE_OUTCOMES")


@dataclass(frozen=True, slots=True)
class IssueEffect:
    """一种结算类别对应的 issue 动作。

    resolve_source：resolve 该资源路径上的全部活动失败记录；
    transition_linked：只改 job 关联的那条 issue，带 CAS。
    """

    kind: Literal["resolve_source", "transition_linked", "none"]
    target: IssueStatus | None = None
    cause: str = ""


# settlement → issue 动作；未列出的类别不改 issue。新增动作型 job 在此加一行。
ISSUE_RULES: dict[Settlement, IssueEffect] = {
    Settlement.INGESTED: IssueEffect("resolve_source"),
    Settlement.ALREADY_INGESTED: IssueEffect("resolve_source"),
    Settlement.DELETE_APPLIED: IssueEffect("resolve_source", cause="source_deleted"),
    Settlement.RESCAN_STILL_PRESENT: IssueEffect("transition_linked", target=IssueStatus.BLOCKED),
    Settlement.RESCAN_CLEARED: IssueEffect("transition_linked", target=IssueStatus.RESOLVED),
}


class _IssueOutcomeRules:
    """成功按动作表流转、失败上报合并——规则实例经注册接进分发器。"""

    def __init__(self, issue_store: IssueStore):
        self._issues = issue_store

    def on_succeeded(
        self,
        job: Job,
        result: JobResult,
        settlement: Settlement,
        conn: sqlite3.Connection,
    ) -> list[Callable[[], None]]:
        effect = ISSUE_RULES.get(settlement)
        if effect is None or effect.kind == "none":
            return []
        if effect.kind == "resolve_source":
            self._resolve_source_failures(job, result, settlement, effect, conn)
        elif job.issue_id:
            self._transition_linked_issue(job, result, settlement, effect, conn)
        return []

    def on_error(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> list[Callable[[], None]]:
        self._on_ingest_error(job, result, conn)
        return []

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
            logger.info("rescan 的 issue %s 已在扫描期间被人工裁决，终态保持人的结论", job.issue_id)

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

    @staticmethod
    def _draft(job: Job, detail: dict) -> IssueDraft:
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


def register_issue_outcomes(outcomes: JobOutcomeHandler, issue_store: IssueStore) -> None:
    """按动作表注册各结算类别，并接管 ingest_error 的失败上报。"""
    rules = _IssueOutcomeRules(issue_store)
    for settlement in ISSUE_RULES:
        outcomes.register_settlement(settlement, rules)
    outcomes.register_error("ingest_error", rules)
