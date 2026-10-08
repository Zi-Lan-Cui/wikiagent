"""问题中心的领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class IssueKind(StrEnum):
    """问题类别，所有上报方共用。"""

    INGESTION_FAILURE = "ingestion_failure"
    RUN_FAILURE = "run_failure"
    QUALITY_ISSUE = "quality_issue"
    CONTENT_CORRECTION = "content_correction"


class IssueStatus(StrEnum):
    """问题的持久化状态。

    没有 processing 状态——是否在处理由 jobs 表中该 issue 是否有在途 job
    派生，此处只表达问题本身的生命周期。
    """

    OPEN = "open"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class IssueSeverity(StrEnum):
    """面向用户的紧急程度，与生命周期状态无关。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


ALLOWED_STATUS_TRANSITIONS: dict[IssueStatus, frozenset[IssueStatus]] = {
    IssueStatus.OPEN: frozenset(
        {
            IssueStatus.BLOCKED,
            IssueStatus.RESOLVED,
            IssueStatus.DISMISSED,
        }
    ),
    IssueStatus.BLOCKED: frozenset(
        {
            IssueStatus.OPEN,
            IssueStatus.RESOLVED,
            IssueStatus.DISMISSED,
        }
    ),
    IssueStatus.RESOLVED: frozenset({IssueStatus.OPEN}),
    IssueStatus.DISMISSED: frozenset({IssueStatus.OPEN}),
}


@dataclass(frozen=True, slots=True)
class IssueDraft:
    """上报方构造的问题草稿，入库前不含 id 等身份字段。"""

    kind: IssueKind
    title: str
    summary: str
    severity: IssueSeverity = IssueSeverity.ERROR
    status: IssueStatus = IssueStatus.OPEN
    fingerprint: str = ""
    origin: JsonObject = field(default_factory=dict)
    resource: JsonObject = field(default_factory=dict)
    diagnostics: JsonObject = field(default_factory=dict)
    retry: JsonObject = field(default_factory=dict)
    evidence: list[JsonObject] = field(default_factory=list)
    context: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IssueRecord:
    """持久化后的完整问题记录，含内部执行上下文。"""

    id: str
    kind: IssueKind
    status: IssueStatus
    severity: IssueSeverity
    title: str
    summary: str
    fingerprint: str
    created_at: str
    updated_at: str
    occurrences: int
    origin: JsonObject
    resource: JsonObject
    diagnostics: JsonObject
    retry: JsonObject
    evidence: list[JsonObject]
    resolution: JsonObject
    context: JsonObject


@dataclass(frozen=True, slots=True)
class IssueAction:
    """服务端授权、可由界面展示的操作。"""

    id: str
    label: str
    style: str = "default"
    requires_confirmation: bool = False
    disabled_reason: str = ""


@dataclass(frozen=True, slots=True)
class IssueCard:
    """面向 CLI 与 Web 的脱敏视图。"""

    id: str
    kind: str
    status: str
    attention: str
    severity: str
    title: str
    summary: str
    created_at: str
    updated_at: str
    occurrences: int
    origin: JsonObject
    resource: JsonObject
    diagnostics: JsonObject
    retry: JsonObject
    evidence: list[JsonObject]
    resolution: JsonObject
    available_actions: tuple[IssueAction, ...]


class IssueNotFoundError(LookupError):
    """问题 id 不存在。"""


class InvalidIssueTransitionError(ValueError):
    """状态转换违反状态机。"""


class IssueActionConflict(RuntimeError):
    """问题当前状态不允许该操作（如已解决仍要求重试），复查状态后可解。"""


class IssueAlreadyClaimedError(RuntimeError):
    """带期望状态的条件更新失败：问题已被并发修改。"""
