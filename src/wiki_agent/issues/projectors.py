"""Sanitized issue-card projections and server-authorized actions."""

from __future__ import annotations

from pathlib import PurePath

from wiki_agent.issues.models import (
    IssueAction,
    IssueCard,
    IssueKind,
    IssueRecord,
    IssueStatus,
    JsonObject,
    JsonValue,
)


def _attention(record: IssueRecord) -> str:
    if record.status in {IssueStatus.RESOLVED, IssueStatus.DISMISSED}:
        return "none"
    if record.kind == IssueKind.INGESTION_FAILURE:
        if record.retry.get("unavailable_reason"):
            return "source_unavailable"
        return "retryable"
    if record.status == IssueStatus.BLOCKED:
        return "decision_required"
    return "decision_required"


def available_actions(record: IssueRecord) -> tuple[IssueAction, ...]:
    """按问题类型与当前状态，推导用户可执行的动作。

    "在途不可重复操作"不在此表达——由提交点的唯一在途/幂等键收敛与
    前端按活跃 job 过滤承担。
    """
    if record.status in {IssueStatus.RESOLVED, IssueStatus.DISMISSED}:
        return (IssueAction("reopen", "重新打开"),)

    actions: list[IssueAction] = []
    if record.kind == IssueKind.INGESTION_FAILURE:
        unavailable_reason = str(record.retry.get("unavailable_reason") or "")
        if unavailable_reason:
            actions.append(
                IssueAction(
                    "retry",
                    "无法重试",
                    disabled_reason=unavailable_reason,
                )
            )
        else:
            actions.append(IssueAction("retry", "重试", style="primary"))
        actions.append(
            IssueAction(
                "open_resource",
                "查看来源",
                disabled_reason=unavailable_reason,
            )
        )
    elif record.kind == IssueKind.CONTENT_CORRECTION:
        actions.extend(
            (
                IssueAction("accept", "确认待修", style="primary"),
                IssueAction("reject", "驳回", requires_confirmation=True),
                IssueAction("keep_uncertain", "保留存疑"),
                IssueAction("open_resource", "查看页面"),
            )
        )
    elif record.kind == IssueKind.QUALITY_ISSUE:
        actions.extend(
            (
                IssueAction("rescan", "重新扫描", style="primary"),
                IssueAction("open_resource", "查看页面"),
                IssueAction("false_positive", "标记误报"),
            )
        )
    else:
        if record.diagnostics.get("log"):
            actions.append(IssueAction("open_log", "查看日志"))
    actions.append(IssueAction("dismiss", "忽略", requires_confirmation=True))
    return tuple(actions)


def _sanitize_path(value: str) -> str:
    if not value:
        return value
    path = PurePath(value)
    return path.name if path.is_absolute() else value.replace("\\", "/").lstrip("/")


def _sanitize(value: JsonValue, *, key: str = "") -> JsonValue:
    if isinstance(value, dict):
        return {
            item_key: _sanitize(item_value, key=item_key) for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, key=key) for item in value]
    if isinstance(value, str) and (key.endswith("path") or key in {"file", "artifact", "log"}):
        return _sanitize_path(value)
    return value


def _sanitize_object(value: JsonObject) -> JsonObject:
    sanitized = _sanitize(value)
    return sanitized if isinstance(sanitized, dict) else {}


def to_card(record: IssueRecord) -> IssueCard:
    """Remove internal execution context and build the adapter DTO."""
    return IssueCard(
        id=record.id,
        kind=record.kind.value,
        status=record.status.value,
        attention=_attention(record),
        severity=record.severity.value,
        title=record.title,
        summary=record.summary,
        created_at=record.created_at,
        updated_at=record.updated_at,
        occurrences=record.occurrences,
        origin=_sanitize_object(record.origin),
        resource=_sanitize_object(record.resource),
        diagnostics=_sanitize_object(record.diagnostics),
        retry=_sanitize_object(record.retry),
        evidence=[_sanitize_object(item) for item in record.evidence],
        resolution=_sanitize_object(record.resolution),
        available_actions=available_actions(record),
    )
