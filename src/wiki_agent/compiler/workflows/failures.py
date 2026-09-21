"""compile/refine 共用的 source 级失败处理。

统一把流水线失败转换成一条 source 级待处理事项；watch 的失败经 Job
结果由应用层的终态联动收口，不走这里的 handler。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.issues import (
    IssueDraft,
    IssueKind,
    IssueService,
    IssueSeverity,
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


def failure_diagnostics(error: Exception) -> tuple[dict[str, Any], str]:
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
        diagnostics, _ = failure_diagnostics(err)
        private_source_path = str(Path(source_path).resolve()) if source_path else ""
        draft = IssueDraft(
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
            context={"source_path": private_source_path},
        )
        # 手动模型：只记 attempt 计数与错因快照，不写任何排程字段
        issue = self._issues.store.report_failure(draft, str(err))
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
