"""Application use cases for actions on persisted Issue records."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from wiki_agent.issues import (
    InvalidIssueTransitionError,
    IssueAlreadyClaimedError,
)
from wiki_agent.issues.models import (
    IssueDraft,
    IssueKind,
    IssueSeverity,
    IssueStatus,
    JsonObject,
)
from wiki_agent.issues.producers import report_quality_findings
from wiki_agent.issues.projectors import available_actions, to_card
from wiki_agent.jobs import Job, JobResult, Settlement
from wiki_agent.jobs.retry_source import SourceUnavailableError, resolve_retry_source
from wiki_agent.wiki.quality import scan_wiki

if TYPE_CHECKING:
    from wiki_agent.application.runtime import AppRuntime
    from wiki_agent.issues.models import IssueCard
    from wiki_agent.issues.service import IssueService


def resolve_correction_issue(
    service: IssueService,
    issue_id: str,
    action: str,
) -> IssueCard:
    """Apply one correction decision; CAS on open/blocked makes double-decide fail loudly."""
    record = service.store.require(issue_id)
    if record.kind != IssueKind.CONTENT_CORRECTION:
        raise ValueError("该问题不是纠错类型")
    if action == "reject":
        target = IssueStatus.RESOLVED
    elif action == "accept":
        service.report(
            IssueDraft(
                kind=IssueKind.QUALITY_ISSUE,
                severity=IssueSeverity.WARNING,
                title=f"{record.resource.get('path') or 'Wiki 页面'}需要修复",
                summary=record.summary,
                fingerprint=f"accepted-correction:{issue_id}",
                origin={"correction_issue_id": issue_id},
                resource=record.resource,
                diagnostics={"error_code": "accepted_correction"},
                evidence=record.evidence,
            )
        )
        target = IssueStatus.RESOLVED
    elif action == "keep_uncertain":
        target = IssueStatus.BLOCKED
    else:
        raise ValueError(f"不支持的纠错操作: {action}")
    return to_card(
        service.store.transition(
            issue_id,
            target,
            resolution={"action": action, "correction_issue_id": issue_id},
            expected={IssueStatus.OPEN, IssueStatus.BLOCKED},
            event="correction_resolved",
        )
    )


class IssueActionExecutor:
    """Execute only actions advertised by the current issue projection.

    retry 不在这里——三个入口（web 问题页、CLI /queue retry、
    scripts/retry_failures.py）统一直投 submit_issue_retry，
    本执行器只承接同步裁决与 rescan。
    """

    def __init__(self, runtime: AppRuntime):
        self.runtime = runtime
        self.service = runtime.issue_service
        self.store = runtime.issue_store

    def reconcile_retry_sources(self) -> int:
        """在适配器启动时标记已丢失的历史重试输入。"""
        changed = 0
        records = self.store.list(
            statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
            kinds={IssueKind.INGESTION_FAILURE},
            limit=1000,
        )
        for record in records:
            if record.retry.get("unavailable_reason"):
                continue
            try:
                resolve_retry_source(record, self.runtime.wiki_dir)
            except SourceUnavailableError as exc:
                self._mark_retry_unavailable(record.id, str(exc))
                changed += 1
        return changed

    def retry_batch_candidates(self, *, exclude_issue_ids: set[str] | None = None) -> list[str]:
        """返回来源有效、可由用户批量触发的重试问题。

        “一键重试”是用户明确发起的操作，因此 manual 策略也应纳入。
        retry policy 只控制无人介入时的自动处理，不应让批量操作与
        单项“重试/重新启用重试”的可用性互相矛盾。
        """
        self.reconcile_retry_sources()
        records = self.store.list(
            statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
            kinds={IssueKind.INGESTION_FAILURE},
            limit=1000,
        )
        candidates: list[str] = []
        seen_sources: set[tuple[str, str]] = set()
        excluded = exclude_issue_ids or set()
        for record in records:
            if record.id in excluded:
                continue
            if record.retry.get("unavailable_reason"):
                continue
            try:
                source = resolve_retry_source(record, self.runtime.wiki_dir)
            except SourceUnavailableError:
                continue
            source_key = (str(record.origin.get("mode") or ""), str(source))
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            actions = {
                item.id: item for item in available_actions(record) if not item.disabled_reason
            }
            if "retry" not in actions:
                continue
            candidates.append(record.id)
        return candidates

    def prepare_retry_batch(self, *, exclude_issue_ids: set[str] | None = None) -> list[str]:
        """为批量入队做最终的来源与动作校验。"""
        selected: list[str] = []
        for issue_id in self.retry_batch_candidates(exclude_issue_ids=exclude_issue_ids):
            self.validate(issue_id, "retry")
            selected.append(issue_id)
        return selected

    def execute(
        self,
        issue_id: str,
        action: str,
        payload: JsonObject | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> IssueCard:
        """同步动作（web 直接裁决）：返回裁决后的问题卡。"""
        if action == "rescan":
            raise ValueError("rescan 的终局裁决在 job 终态事务内完成，只能经队列执行")
        return self._run_action(issue_id, action, payload, progress=progress)[0]

    def job_effect(
        self,
        issue_id: str,
        action: str,
        payload: JsonObject | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> JsonObject:
        """issue_action job 的执行入口：返回要随 job 终态落库的 JobResult detail。

        rescan 的最终结论（blocked/resolved）不由这里直接写库——这里只产出
        判断依据，issue 终态统一在 JobOutcomeHandler 的终态事务里写入，
        issue 状态的写入方保持唯一。
        """
        return self._run_action(issue_id, action, payload, progress=progress)[1]

    def _run_action(
        self,
        issue_id: str,
        action: str,
        payload: JsonObject | None,
        *,
        progress: Callable[[str], None] | None,
    ) -> tuple[IssueCard, JsonObject]:
        self.validate(issue_id, action)
        record = self.store.require(issue_id)
        allowed = {item.id for item in available_actions(record) if not item.disabled_reason}
        if action not in allowed:
            raise ValueError(f"当前问题不允许操作: {action}")
        if action in {"dismiss", "reopen"}:
            return self.service.apply_simple_action(issue_id, action, payload), {}
        if action == "open_resource" or action == "open_log":
            return to_card(record), {}
        if action in {"accept", "reject", "keep_uncertain"}:
            return resolve_correction_issue(self.service, record.id, action), {}
        if action == "rescan":
            return self._rescan(record.id, progress=progress)
        if action == "false_positive":
            return (
                to_card(
                    self.store.transition(
                        record.id,
                        IssueStatus.DISMISSED,
                        resolution={"action": "false_positive"},
                        event="marked_false_positive",
                    )
                ),
                {},
            )
        raise ValueError(f"尚未实现操作: {action}")

    def validate(self, issue_id: str, action: str) -> None:
        """在建立后台任务前验证操作与重试资源。"""
        record = self.store.require(issue_id)
        unavailable_reason = str(record.retry.get("unavailable_reason") or "")
        if action == "retry" and unavailable_reason:
            raise SourceUnavailableError(unavailable_reason)
        allowed = {item.id for item in available_actions(record) if not item.disabled_reason}
        if action not in allowed:
            raise ValueError(f"当前问题不允许操作: {action}")
        if action != "retry":
            return
        try:
            resolve_retry_source(record, self.runtime.wiki_dir)
        except SourceUnavailableError as exc:
            self._mark_retry_unavailable(record.id, str(exc))
            raise

    def _mark_retry_unavailable(self, issue_id: str, reason: str) -> None:
        record = self.store.require(issue_id)
        self.store.update_payloads(
            issue_id,
            retry={**record.retry, "unavailable_reason": reason},
            diagnostics={**record.diagnostics, "detail": reason},
            event="retry_source_unavailable",
        )
        if record.status == IssueStatus.OPEN:
            self.store.transition(
                issue_id,
                IssueStatus.BLOCKED,
                event="blocked_source_unavailable",
            )

    def _rescan(
        self,
        issue_id: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[IssueCard, JsonObject]:
        """复扫全库、同步质量问题账——只产出裁决依据，不写 issue 终态。

        still_present 由复扫前后的 occurrences 对比得出（入账器按指纹合并，
        仍在=计数前进）。终态写入统一在 JobOutcomeHandler 的 job 终态事务
        （CAS：expected={open,blocked}），扫描期间的人工裁决不被覆盖。
        防重复由承载它的 issue_action job 保证：同 issue 的在途行被幂等键收敛。
        """
        before = self.store.require(issue_id)
        if progress is not None:
            progress("scan")
        findings = scan_wiki(self.runtime.wiki_dir)
        if progress is not None:
            progress("同步质量问题")
        report_quality_findings(
            self.service,
            findings,
            origin={"mode": "web", "trigger": "issue_rescan"},
        )
        after_scan = self.store.require(issue_id)
        still_present = after_scan.occurrences > before.occurrences
        return to_card(after_scan), {
            "settlement": (
                Settlement.RESCAN_STILL_PRESENT if still_present else Settlement.RESCAN_CLEARED
            ),
            "rescan_findings": len(findings),
        }


class IssueActionJobHandler:
    """issue_action job 的执行体（装配根注册进 JobWorker，适配器只做 HTTP/入口映射）。

    业务拒绝（来源丢失/动作非法/未实现）是终态——返回无联动语义的
    failed 结果，问题账本不因被拒绝的动作而新增记录。rescan 的 issue
    终态不在这里写：job_effect 只产出判断依据，终态统一由 outcome 在
    job 终态事务写入。
    """

    def __init__(self, executor: IssueActionExecutor):
        self._executor = executor

    async def __call__(self, job: Job, progress) -> JobResult:
        try:
            # job payload 来自 json 落库，运行时即 JsonObject 形状
            detail = self._executor.job_effect(
                job.resource, job.mode, cast("JsonObject | None", job.payload), progress=progress
            )
        except (
            SourceUnavailableError,
            IssueAlreadyClaimedError,
            InvalidIssueTransitionError,
            ValueError,
        ) as exc:
            return JobResult(status="failed", detail={"error": str(exc)[:500]})
        effect_detail: dict[str, object] = {k: v for k, v in detail.items()}
        return JobResult(status="succeeded", detail=effect_detail)
