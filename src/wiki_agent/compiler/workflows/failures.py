"""compile/refine 共用的 source 级失败处理。

Pipeline 内部保留 ``IngestError.stage/raw`` 的细节；边界统一把失败
转换成一条 source 级待处理事项。watch 有自己的实时失败通道，不使用本模块。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from wiki_agent.config import RetryConfig
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.issues import (
    IssueDraft,
    IssueKind,
    IssueRecord,
    IssueService,
    IssueSeverity,
    IssueStatus,
    IssueStore,
)
from wiki_agent.log import emit_event, get_logger


def _find_ingest_error(error: Exception) -> IngestError | None:
    """在异常链中定位保留了阶段和原始输出的 ingestion 异常。"""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, IngestError):
            return current
        nested = getattr(current, "cause", None)
        current = nested if isinstance(nested, BaseException) else current.__cause__
    return None


def _failure_diagnostics(error: Exception) -> tuple[dict[str, Any], str]:
    """生成可持久化/展示的诊断，并将原始模型输出单独返回给事件日志。"""
    ingest_error = _find_ingest_error(error)
    diagnostics: dict[str, Any] = {"detail": str(error)[:1000]}
    raw = ""
    if ingest_error is None:
        return diagnostics, raw

    diagnostics.update(
        {
            "stage": ingest_error.stage.value,
            "error_code": ingest_error.error_code,
            "error_class": ingest_error.error_class,
        }
    )
    raw = ingest_error.raw
    if not raw:
        return diagnostics, raw

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return diagnostics, raw
    if not isinstance(payload, list):
        return diagnostics, raw

    failures: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")[:500]
        reason = str(item.get("error") or "")[:1000]
        if path or reason:
            # item["raw"] 可能是大段 LLM 输出，只允许进入事件日志。
            failures.append({"path": path, "reason": reason})
    if failures:
        diagnostics["failures"] = failures
    return diagnostics, raw


class SourceFailureHandler:
    """将 compile/refine/watch 失败写入统一问题库。"""

    def __init__(
        self,
        issue_service: IssueService,
        *,
        mode: str,
    ):
        if mode not in {"compile", "refine", "watch"}:
            raise ValueError(f"source failure 不支持的 mode: {mode!r}")
        self._issues = issue_service
        self._mode = mode
        self._retry_mode = "compile" if mode == "watch" else mode
        self._logger = get_logger(f"{mode.upper()}_FAILURE")
        self._retry_window = timedelta(hours=24)

    def handle(
        self,
        error: IngestError | Exception,
        *,
        source: str,
        source_path: str | Path = "",
        source_kind: str = "input_file",
    ) -> IngestError:
        """记录一次 source 失败并返回标准化的 ``IngestError``。"""
        err = (
            error
            if isinstance(error, IngestError)
            else IngestError(
                IngestStage.LOAD,
                f"未分类: {error}",
                source=source,
                cause=error,
            )
        )
        stage = err.stage.value
        diagnostics, _ = _failure_diagnostics(err)
        retry_expires_at = (datetime.now() + self._retry_window).isoformat()
        private_source_path = str(Path(source_path).resolve()) if source_path else ""
        issue = self._issues.report(
            IssueDraft(
                kind=IssueKind.INGESTION_FAILURE,
                severity=IssueSeverity.ERROR,
                title=f"{source or '来源文件'}处理失败",
                summary=str(err)[:500],
                origin={"mode": self._retry_mode, "reported_by": self._mode, "stage": stage},
                resource={
                    "type": source_kind,
                    "path": source,
                    "label": source,
                },
                diagnostics={
                    **diagnostics,
                },
                retry={
                    "policy": err.retry_policy,
                    "attempts": 1,
                    "next_retry_at": "",
                    "expires_at": retry_expires_at,
                },
                context={"source_path": private_source_path},
            )
        )
        emit_event(
            "ingest_failure",
            issue_id=issue.id,
            mode=self._mode,
            source_kind=source_kind,
            file=source,
            source_path=str(source_path),
            stage=stage,
            error=str(err),
            cause=type(err.cause).__name__ if err.cause else None,
            raw=err.raw,
        )
        self._logger.error(
            "source 失败 [%s] %s: %s",
            stage,
            source,
            str(err)[:200],
        )
        return err


class SourceFailureConsumer:
    """数据库中 source 失败问题的重试策略内核。

    ``processor`` 从 source 起点重新执行；消费者只负责策略、次数和
    队列状态，避免把 compile/refine 两套流水线复制进队列层。
    """

    def __init__(
        self,
        store: IssueStore,
        processor,
        *,
        retry_config: RetryConfig | None = None,
        max_attempts: int | None = None,
        base_delay_seconds: float | None = None,
        max_delay_seconds: float | None = None,
    ):
        retry = retry_config or RetryConfig()
        self._store = store
        self._processor = processor
        self._max_attempts = max_attempts if max_attempts is not None else retry.source_max_attempts
        self._base_delay_seconds = (
            base_delay_seconds
            if base_delay_seconds is not None
            else retry.source_base_delay_seconds
        )
        self._max_delay_seconds = (
            max_delay_seconds if max_delay_seconds is not None else retry.source_max_delay_seconds
        )

    def classify(self, issue: IssueRecord) -> str:
        policy = issue.retry.get("policy", "manual")
        attempts_value = issue.retry.get("attempts", 0)
        attempts = int(attempts_value) if isinstance(attempts_value, (str, int, float)) else 0
        if self._is_expired(issue):
            return "manual"
        if policy == "auto_retry" and attempts < self._max_attempts:
            return "retry"
        if policy == "retry_once" and attempts < min(self._max_attempts, 2):
            return "retry_once"
        return "manual"

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _is_expired(self, issue: IssueRecord) -> bool:
        deadline = self._parse_time(str(issue.retry.get("expires_at", "")))
        return deadline is not None and datetime.now() >= deadline

    def _is_deferred(self, issue: IssueRecord) -> bool:
        next_retry = self._parse_time(str(issue.retry.get("next_retry_at", "")))
        return next_retry is not None and datetime.now() < next_retry

    async def consume(self, issue: IssueRecord, *, force: bool = False) -> dict:
        """执行一个 source 问题，并将诊断与重试计数写回数据库。"""
        decision = "retry" if force else self.classify(issue)
        if decision == "manual":
            if issue.status == IssueStatus.OPEN:
                self._store.transition(
                    issue.id, IssueStatus.BLOCKED, event="retry_requires_decision"
                )
            return {"id": issue.id, "status": "manual"}
        if not force and self._is_deferred(issue):
            return {
                "id": issue.id,
                "status": "deferred",
                "next_retry_at": issue.retry.get("next_retry_at", ""),
            }

        attempts_value = issue.retry.get("attempts", 0)
        attempts = (int(attempts_value) if isinstance(attempts_value, (str, int, float)) else 0) + 1
        try:
            await self._processor(issue)
        except Exception as exc:
            diagnostics, raw = _failure_diagnostics(exc)
            expired = self._is_expired(issue)
            policy_limit = (
                min(self._max_attempts, 2)
                if issue.retry.get("policy") == "retry_once"
                else self._max_attempts
            )
            retryable = attempts < policy_limit and not expired
            delay = min(
                self._max_delay_seconds,
                self._base_delay_seconds * (2 ** max(0, attempts - 1)),
            )
            next_retry_at = (
                (datetime.now() + timedelta(seconds=delay)).isoformat() if retryable else ""
            )
            self._store.update_payloads(
                issue.id,
                retry={
                    **issue.retry,
                    "attempts": attempts,
                    "next_retry_at": next_retry_at,
                    "last_error": str(exc)[:500],
                },
                diagnostics={**issue.diagnostics, **diagnostics},
                event="source_retry_failed",
            )
            emit_event(
                "source_retry_failed",
                issue_id=issue.id,
                attempts=attempts,
                error=str(exc),
                diagnostics=diagnostics,
                raw=raw,
                next_retry_at=next_retry_at,
            )
            return {
                "id": issue.id,
                "status": "failed",
                "attempts": attempts,
                "next_retry_at": next_retry_at,
                "error": str(exc)[:500],
                "diagnostics": diagnostics,
            }
        self._store.update_payloads(
            issue.id,
            retry={**issue.retry, "attempts": attempts, "next_retry_at": "", "last_error": ""},
            event="source_retry_succeeded",
        )
        emit_event("source_retry_succeeded", issue_id=issue.id, attempts=attempts)
        return {"id": issue.id, "status": "succeeded", "attempts": attempts}
