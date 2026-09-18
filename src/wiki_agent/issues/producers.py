"""Shared adapters from runtime/compiler findings to issue drafts."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from wiki_agent.issues.models import (
    IssueDraft,
    IssueKind,
    IssueSeverity,
    JsonObject,
    JsonValue,
)
from wiki_agent.issues.service import IssueService

if TYPE_CHECKING:
    from wiki_agent.compiler.restructure.models import Conflict, Proposal


class QualityFinding(Protocol):
    level: str
    path: str
    message: str


def _proposal_payload(proposal: Proposal) -> JsonObject:
    pages: list[JsonValue] = list(proposal.pages)
    return {
        "op": proposal.op,
        "pages": pages,
        "target": proposal.target,
        "reason": proposal.reason,
    }


def report_correction(
    service: IssueService,
    *,
    text: str,
    page: str = "",
    session_id: str = "",
) -> str:
    """Persist a user correction directly in the issue database."""
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
    """Persist scan findings, promoting Disputed markers to content conflicts."""
    issue_ids: list[str] = []
    for finding in findings:
        conflict = "Disputed" in finding.message or "矛盾" in finding.message
        kind = IssueKind.CONTENT_CONFLICT if conflict else IssueKind.QUALITY_ISSUE
        card = service.report(
            IssueDraft(
                kind=kind,
                severity=(
                    IssueSeverity.ERROR if finding.level == "error" else IssueSeverity.WARNING
                ),
                title=(
                    f"{finding.path} 存在内容冲突" if conflict else f"{finding.path} 未通过质量检查"
                ),
                summary=finding.message,
                origin={**(origin or {}), "stage": "scan"},
                resource={"type": "wiki_page", "path": finding.path, "label": finding.path},
                diagnostics={"error_code": "content_conflict" if conflict else "quality_scan"},
                evidence=[{"key": finding.message, "path": finding.path, "claim": finding.message}],
            )
        )
        issue_ids.append(card.id)
    return issue_ids


def report_restructure_conflicts(
    service: IssueService,
    conflicts: Iterable[Conflict],
    *,
    origin: JsonObject | None = None,
) -> list[str]:
    """Persist unresolved destructive proposals as user decisions."""
    issue_ids: list[str] = []
    for conflict in conflicts:
        proposals: list[JsonValue] = [
            _proposal_payload(proposal) for proposal in conflict.proposals
        ]
        pages = sorted({page for proposal in conflict.proposals for page in proposal.pages})
        card = service.report(
            IssueDraft(
                kind=IssueKind.RESTRUCTURE_CONFLICT,
                severity=IssueSeverity.WARNING,
                title="Wiki 结构提案无法自动仲裁",
                summary=conflict.detail or "多个高风险结构操作存在冲突。",
                fingerprint=f"restructure:{conflict.kind}:{'|'.join(pages)}",
                origin={**(origin or {}), "stage": "arbitration"},
                resource={
                    "type": "wiki_structure",
                    "path": pages[0] if pages else "",
                    "label": conflict.kind,
                },
                diagnostics={"error_code": "restructure_conflict"},
                evidence=[{"key": conflict.detail or conflict.kind, "proposals": proposals}],
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
    """Record a boundary-level failure that has no source-level owner."""
    card = service.report(
        IssueDraft(
            kind=IssueKind.RUN_FAILURE,
            severity=IssueSeverity.ERROR,
            title=title,
            summary=f"{type(error).__name__}: {error}"[:1000],
            origin=origin,
            resource=resource or {},
            diagnostics={
                "error_code": "run_failure",
                "error_class": type(error).__name__,
                "detail": str(error)[:1000],
            },
            evidence=[{"key": f"{type(error).__name__}:{str(error)[:200]}"}],
        )
    )
    return card.id
