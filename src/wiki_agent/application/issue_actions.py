"""Application use cases for actions on persisted Issue records."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from wiki_agent.compiler.workflows.retry import (
    SourceUnavailableError,
    resolve_retry_source,
    retry_source_failures,
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
    """Apply one correction decision through the shared audited workflow."""
    record = service.store.require(issue_id)
    if record.kind != IssueKind.CONTENT_CORRECTION:
        raise ValueError("该问题不是纠错类型")
    action_id = service.store.claim_action(issue_id, action)
    try:
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
            service.store.complete_action(
                action_id,
                status=target,
                result={"action": action, "correction_issue_id": issue_id},
            )
        )
    except Exception as exc:
        service.store.fail_action(action_id, f"{type(exc).__name__}: {exc}")
        raise


class IssueActionExecutor:
    """Execute only actions advertised by the current issue projection."""

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

    async def execute(
        self,
        issue_id: str,
        action: str,
        payload: JsonObject | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> IssueCard:
        self.validate(issue_id, action)
        record = self.store.require(issue_id)
        allowed = {item.id for item in available_actions(record) if not item.disabled_reason}
        if action not in allowed:
            raise ValueError(f"当前问题不允许操作: {action}")
        if action in {"dismiss", "reopen"}:
            return self.service.apply_simple_action(issue_id, action, payload)
        if action == "open_resource" or action == "open_log":
            return to_card(record)
        if action == "retry":
            return await self._retry_ingestion(record.id, progress=progress)
        if action in {"accept", "reject", "keep_uncertain"}:
            return self._resolve_correction(record.id, action)
        if action == "rescan":
            return self._rescan(record.id, progress=progress)
        if action == "false_positive":
            return to_card(
                self.store.transition(
                    record.id,
                    IssueStatus.DISMISSED,
                    resolution={"action": "false_positive"},
                    event="marked_false_positive",
                )
            )
        if action == "keep_disputed" or action == "defer":
            return to_card(
                self.store.transition(
                    record.id,
                    IssueStatus.BLOCKED,
                    resolution={"action": action},
                    event="deferred",
                )
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

    async def _retry_ingestion(
        self,
        issue_id: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> IssueCard:
        record = self.store.require(issue_id)
        if record.kind != IssueKind.INGESTION_FAILURE:
            raise ValueError("只有资料处理失败问题可以执行来源重试")
        action_id = self.store.claim_action(issue_id, "retry")
        try:
            result = await retry_source_failures(
                self.store,
                llm=self.runtime.agent.llm,
                vlm=self.runtime.agent.vlm,
                wiki_dir=self.runtime.wiki_dir,
                source_records_dir=self.runtime.source_records_dir,
                run_root=self.runtime.runs_dir,
                compile_config=self.runtime.config.compile,
                retry_config=self.runtime.config.retry,
                issue_id=issue_id,
                on_progress=progress,
                force=True,
            )
        except SourceUnavailableError as exc:
            reason = str(exc)
            self._mark_retry_unavailable(issue_id, reason)
            self.store.fail_action(action_id, reason, blocked=True)
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            latest = self.store.require(issue_id)
            self.store.update_payloads(
                issue_id,
                diagnostics={
                    **latest.diagnostics,
                    "detail": message,
                    "error_code": "retry_execution_failed",
                },
                retry={**latest.retry, "last_error": message},
                event="retry_diagnostics_updated",
            )
            self.store.fail_action(action_id, message)
            raise
        succeeded = any(
            item.get("id") == issue_id and item.get("status") == "succeeded"
            for item in result.get("results", [])
        )
        if result.get("committed") and succeeded:
            return to_card(
                self.store.complete_action(
                    action_id,
                    status=IssueStatus.RESOLVED,
                    result={"action": "retry", "result": "succeeded"},
                )
            )
        message = str(result.get("message") or "重试未完成，Wiki 已回撤")
        diagnostics_value = result.get("diagnostics")
        failed_result = next(
            (
                item
                for item in result.get("results", [])
                if item.get("id") == issue_id and item.get("status") == "failed"
            ),
            None,
        )
        latest = self.store.require(issue_id)
        diagnostics = dict(latest.diagnostics)
        if isinstance(diagnostics_value, dict):
            diagnostics.update(cast(JsonObject, diagnostics_value))
        diagnostics["detail"] = str(diagnostics.get("detail") or message)
        retry = dict(latest.retry)
        retry["last_error"] = message
        if failed_result is not None:
            retry["attempts"] = int(failed_result.get("attempts", 0) or 0)
        self.store.update_payloads(
            issue_id,
            diagnostics=diagnostics,
            retry=retry,
            event="retry_diagnostics_updated",
        )
        self.store.fail_action(
            action_id,
            message,
            blocked=not result.get("results"),
        )
        raise RuntimeError(message)

    def _resolve_correction(self, issue_id: str, action: str) -> IssueCard:
        return resolve_correction_issue(
            self.service,
            issue_id,
            action,
        )

    def _rescan(
        self,
        issue_id: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> IssueCard:
        before = self.store.require(issue_id)
        action_id = self.store.claim_action(issue_id, "rescan")
        try:
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
            return to_card(
                self.store.complete_action(
                    action_id,
                    status=IssueStatus.BLOCKED if still_present else IssueStatus.RESOLVED,
                    result={
                        "action": "rescan",
                        "findings": len(findings),
                        "still_present": still_present,
                    },
                )
            )
        except Exception as exc:
            self.store.fail_action(action_id, f"{type(exc).__name__}: {exc}")
            raise
