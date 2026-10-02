"""把运行时与扫描结果转为 IssueDraft 并上报。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from wiki_agent.errors import (
    ERROR_DETAIL_LIMIT,
    ERROR_SUMMARY_LIMIT,
    ERROR_TRACE_LIMIT,
    summarize_error,
)
from wiki_agent.issues.models import (
    IssueDraft,
    IssueKind,
    IssueSeverity,
    JsonObject,
)
from wiki_agent.issues.service import IssueService


class QualityFinding(Protocol):
    level: str
    path: str
    message: str


def report_correction(
    service: IssueService,
    *,
    text: str,
    page: str = "",
    session_id: str = "",
) -> str:
    """把用户纠错直接记为 issue。"""
    correction_id = f"correction_{uuid4().hex}"
    page = page.strip()
    text = text.strip()
    card = service.report(
        IssueDraft(
            kind=IssueKind.CONTENT_CORRECTION,
            severity=IssueSeverity.WARNING,
            title=f"{page or 'Wiki 内容'}收到纠错",
            summary=text or "用户提交了一条 Wiki 纠错。",
            fingerprint=correction_id,
            origin={
                "session_id": session_id,
                "created_at": datetime.now(UTC).isoformat(),
            },
            resource={
                "type": "wiki_page" if page else "unknown",
                "path": page,
                "label": page,
            },
            evidence=[{"key": correction_id, "claim": text}],
        )
    )
    return card.id


def report_quality_findings(
    service: IssueService,
    findings: Iterable[QualityFinding],
    *,
    origin: JsonObject | None = None,
) -> list[str]:
    """扫描发现记为质量问题；内容正确性由用户裁决（复核/忽略）。"""
    issue_ids: list[str] = []
    for finding in findings:
        card = service.report(
            IssueDraft(
                kind=IssueKind.QUALITY_ISSUE,
                severity=(
                    IssueSeverity.ERROR if finding.level == "error" else IssueSeverity.WARNING
                ),
                title=f"{finding.path} 未通过质量检查",
                summary=finding.message,
                origin={**(origin or {}), "stage": "scan"},
                resource={"type": "wiki_page", "path": finding.path, "label": finding.path},
                diagnostics={"error_code": "quality_scan"},
                evidence=[{"key": finding.message, "path": finding.path, "claim": finding.message}],
            )
        )
        issue_ids.append(card.id)
    return issue_ids


def report_run_failure(
    service: IssueService,
    *,
    title: str,
    error: Exception,
    origin: JsonObject,
    resource: JsonObject | None = None,
) -> str:
    """记录没有明确归属来源的运行失败。"""
    card = service.report(
        IssueDraft(
            kind=IssueKind.RUN_FAILURE,
            severity=IssueSeverity.ERROR,
            title=title,
            summary=summarize_error(f"{type(error).__name__}: {error}", ERROR_SUMMARY_LIMIT),
            origin=origin,
            resource=resource or {},
            diagnostics={
                "error_code": "run_failure",
                "error_class": type(error).__name__,
                "detail": summarize_error(error, ERROR_DETAIL_LIMIT),
            },
            evidence=[
                {"key": f"{type(error).__name__}:{summarize_error(error, ERROR_TRACE_LIMIT)}"}
            ],
        )
    )
    return card.id
