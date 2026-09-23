"""Job 终态 → Issue 账本 / SyncState 的唯一联动点。

由 JobService.complete_with_outcome 在终态事务内调用 apply(job, result, conn)：
一切跨表写都并入该事务。SyncState 是 JSON 文件、参与不了 SQLite
事务——apply 返回"提交后动作"清单，由 service 在 commit 之后立即执行
（先库后文件：崩溃窗口靠 recover_stale 与 digest 幂等短路收敛，方向
只能是"库里没记成就重做"）。

issue 联动的唯一驱动源是成功结果的 settlement（结算类别）：handler 申报
"完成了哪一种业务事实"，本模块查 ISSUE_RULES 决定账本动作——不存在
"job 成功就一律销账"的通用规则，也没有 handler 直接写账的路径。
未申报/表里没有的类别不动账本。

手动重试模型：失败只做记账——issue 停在 open（attempts 计数、
last_error 快照），不排任何程。人修好环境后再次 sync 即重试；issue 的
retry 按钮走 submit_issue_retry 直投一次性尝试。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
from wiki_agent.jobs import Job, JobResult, Settlement
from wiki_agent.log import emit_event, get_logger

if TYPE_CHECKING:
    from wiki_agent.sync.state import SyncState

logger = get_logger("JOB_OUTCOMES")


@dataclass(frozen=True, slots=True)
class IssueEffect:
    """一种结算类别对问题账本的动作描述。

    resolve_source：解决"该资源路径"的全部活动失败记录（不止 job 挂的那条
    ——对象已被处理掉时，同源的旧账一起失效）；
    transition_linked：只动 job 挂账的那条 issue，带 CAS。
    """

    kind: Literal["resolve_source", "transition_linked", "none"]
    target: IssueStatus | None = None
    cause: str = ""


# 结算类别 → 账本动作。没列出的类别（refined/unit_missing/applied/
# rejected_by_gate）= 不碰账本；新增动作型 job 在这里加一行，不改分支。
ISSUE_RULES: dict[Settlement, IssueEffect] = {
    Settlement.INGESTED: IssueEffect("resolve_source"),
    Settlement.ALREADY_INGESTED: IssueEffect("resolve_source"),
    Settlement.DELETE_APPLIED: IssueEffect("resolve_source", cause="source_deleted"),
    Settlement.RESCAN_STILL_PRESENT: IssueEffect(
        "transition_linked", target=IssueStatus.BLOCKED
    ),
    Settlement.RESCAN_CLEARED: IssueEffect("transition_linked", target=IssueStatus.RESOLVED),
}


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
            self._apply_issue_rule(job, result, conn)
        elif result.error_type == "ingest_error":
            self._on_ingest_error(job, result, conn)
        # 其余（cancelled、handler bug 的无联动 failed）刻意零动作
        return post_commit

    # settlement → 账本动作（规则表在模块顶部）

    def _apply_issue_rule(self, job: Job, result: JobResult, conn: sqlite3.Connection) -> None:
        raw = str(result.detail.get("settlement") or "")
        if not raw:
            return  # 未申报 = 无联动语义的成功（协议如此，不猜）
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
        """解决该资源路径上的全部活动失败记录（open/blocked）。

        对象被处理掉时同源旧账一起失效，不止 job 挂的那条；resolution 只留
        小的可追溯字段——detail 里的全文（text/source_page/archive_ops）不进账本。
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
        """只动 job 挂账的那条 issue，CAS 挡住扫描窗口内的人工裁决。

        （当前唯一使用者是 rescan：executor 只产出复扫结论，终态在这里落。）
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
