"""Domain models for the unified issue center."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class IssueKind(StrEnum):
    """Stable categories shared by all issue producers."""

    INGESTION_FAILURE = "ingestion_failure"
    RUN_FAILURE = "run_failure"
    QUALITY_ISSUE = "quality_issue"
    CONTENT_CORRECTION = "content_correction"
    CONTENT_CONFLICT = "content_conflict"
    RESTRUCTURE_CONFLICT = "restructure_conflict"


class IssueStatus(StrEnum):
    """Persistent issue lifecycle states."""

    OPEN = "open"
    PROCESSING = "processing"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class IssueSeverity(StrEnum):
    """User-facing urgency independent from lifecycle state."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


ALLOWED_STATUS_TRANSITIONS: dict[IssueStatus, frozenset[IssueStatus]] = {
    IssueStatus.OPEN: frozenset(
        {
            IssueStatus.PROCESSING,
            IssueStatus.BLOCKED,
            IssueStatus.RESOLVED,
            IssueStatus.DISMISSED,
        }
    ),
    IssueStatus.PROCESSING: frozenset(
        {
            IssueStatus.OPEN,
            IssueStatus.BLOCKED,
            IssueStatus.RESOLVED,
            IssueStatus.DISMISSED,
        }
    ),
    IssueStatus.BLOCKED: frozenset(
        {
            IssueStatus.OPEN,
            IssueStatus.PROCESSING,
            IssueStatus.RESOLVED,
            IssueStatus.DISMISSED,
        }
    ),
    IssueStatus.RESOLVED: frozenset({IssueStatus.OPEN}),
    IssueStatus.DISMISSED: frozenset({IssueStatus.OPEN}),
}


@dataclass(frozen=True, slots=True)
class IssueDraft:
    """A producer-owned issue report before persistence assigns identity."""

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
    """The complete persisted issue, including internal execution context."""

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
    """One server-authorized decision that can be shown by an adapter."""

    id: str
    label: str
    style: str = "default"
    requires_confirmation: bool = False
    disabled_reason: str = ""


@dataclass(frozen=True, slots=True)
class IssueCard:
    """Sanitized issue projection exposed to CLI and Web adapters."""

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
    """Raised when an issue id is unknown."""


class InvalidIssueTransitionError(ValueError):
    """Raised when a lifecycle transition violates the state machine."""


class IssueAlreadyClaimedError(RuntimeError):
    """Raised when another worker has already claimed an issue action."""
