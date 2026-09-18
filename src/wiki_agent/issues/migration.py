"""Idempotent migration from legacy queue and correction JSONL files."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from wiki_agent.issues.models import (
    IssueDraft,
    IssueKind,
    IssueSeverity,
    IssueStatus,
    JsonObject,
)
from wiki_agent.issues.store import IssueStore


def _read_jsonl(path: Path) -> list[JsonObject]:
    if not path.is_file():
        return []
    records: list[JsonObject] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _backup_once(path: Path) -> None:
    if not path.is_file():
        return
    backup = path.with_name(f"{path.name}.legacy.bak")
    if not backup.exists():
        shutil.copy2(path, backup)


def _text(record: JsonObject, key: str, default: str = "") -> str:
    value = record.get(key, default)
    return str(value) if value is not None else default


def _integer(record: JsonObject, key: str, default: int = 0) -> int:
    value = record.get(key, default)
    if not isinstance(value, (str, int, float, bool)):
        return default
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _expired(value: str) -> bool:
    if not value:
        return False
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return False
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo is not None else datetime.now()
    return now >= deadline


def _legacy_queue_draft(record: JsonObject) -> IssueDraft:
    legacy_type = _text(record, "type")
    file_name = _text(record, "file")
    source_path = _text(record, "source_path")
    detail = _text(record, "error") or _text(record, "detail") or _text(record, "issue")
    if legacy_type == "ingest_failure":
        kind = IssueKind.INGESTION_FAILURE
        title = f"{file_name or '来源文件'}处理失败"
        summary = detail or "来源文件未能完成知识编译。"
    elif legacy_type in {"restructure_conflict", "surgery_conflict"}:
        # "surgery_conflict" 是改名前的历史队列 type，保留兼容读取。
        kind = IssueKind.RESTRUCTURE_CONFLICT
        title = "Wiki 重组提案存在冲突"
        summary = detail or "多个修改提案无法自动仲裁。"
    elif legacy_type == "wiki_issue":
        kind = IssueKind.CONTENT_CORRECTION
        title = f"{file_name or 'Wiki 页面'}需要修正"
        summary = _text(record, "issue") or detail or "已确认的 Wiki 内容问题。"
    else:
        kind = IssueKind.RUN_FAILURE
        title = f"{legacy_type or '旧任务'}需要处理"
        summary = detail or "由旧异常队列迁移的待处理事项。"

    retry_expires_at = _text(record, "retry_expires_at")
    old_status = _text(record, "status", "pending")
    status = IssueStatus.OPEN
    if old_status in {"succeeded", "resolved", "done"}:
        status = IssueStatus.RESOLVED
    elif old_status in {"manual", "blocked"} or _expired(retry_expires_at):
        status = IssueStatus.BLOCKED

    relative_path = file_name or (Path(source_path).name if source_path else "")
    origin: JsonObject = {
        "mode": _text(record, "mode") or _text(record, "source"),
        "stage": _text(record, "stage"),
        "legacy_queue_id": _text(record, "id"),
    }
    resource: JsonObject = {
        "type": _text(record, "source_kind", "unknown"),
        "path": relative_path,
        "label": file_name or relative_path,
    }
    diagnostics: JsonObject = {
        "error_code": _text(record, "error_code"),
        "error_class": _text(record, "error_class"),
        "detail": detail[:1000],
    }
    retry: JsonObject = {
        "policy": _text(record, "retry_policy", "manual"),
        "attempts": _integer(record, "attempts", 0),
        "next_retry_at": _text(record, "next_retry_at"),
        "expires_at": retry_expires_at,
        "last_error": _text(record, "last_error")[:1000],
    }
    evidence: list[JsonObject] = []
    proposals = record.get("proposals")
    if isinstance(proposals, list):
        evidence.append({"key": "legacy_proposals", "proposals": proposals})
    return IssueDraft(
        kind=kind,
        status=status,
        severity=(
            IssueSeverity.WARNING
            if kind in {IssueKind.CONTENT_CORRECTION, IssueKind.RESTRUCTURE_CONFLICT}
            else IssueSeverity.ERROR
        ),
        title=title,
        summary=summary,
        fingerprint=f"legacy-queue:{_text(record, 'id')}",
        origin=origin,
        resource=resource,
        diagnostics=diagnostics,
        retry=retry,
        evidence=evidence,
        context={"source_path": source_path},
    )


def _correction_draft(record: JsonObject) -> IssueDraft:
    correction_id = _text(record, "id")
    page = _text(record, "page")
    text = _text(record, "text")
    legacy_status = _text(record, "status", "pending")
    status = IssueStatus.BLOCKED if legacy_status == "uncertain" else IssueStatus.OPEN
    return IssueDraft(
        kind=IssueKind.CONTENT_CORRECTION,
        status=status,
        severity=IssueSeverity.WARNING,
        title=f"{page or 'Wiki 内容'}收到纠错",
        summary=text or "用户提交了一条 Wiki 纠错。",
        fingerprint=f"legacy-correction:{correction_id}",
        origin={
            "session_id": _text(record, "session_key"),
            "legacy_correction_id": correction_id,
        },
        resource={"type": "wiki_page" if page else "unknown", "path": page, "label": page},
        evidence=[{"key": correction_id, "claim": text}],
    )


def migrate_legacy_issues(workspace: str | Path, store: IssueStore | None = None) -> dict[str, int]:
    """Import each legacy record once and preserve first-seen source backups."""
    root = Path(workspace)
    issue_store = store or IssueStore(root)
    queue_file = root / "queue.jsonl"
    correction_file = root / "memory_store" / "corrections.jsonl"
    _backup_once(queue_file)
    _backup_once(correction_file)

    counts = {"queue": 0, "corrections": 0, "skipped": 0}
    for source, path, factory in (
        ("queue", queue_file, _legacy_queue_draft),
        ("corrections", correction_file, _correction_draft),
    ):
        for index, record in enumerate(_read_jsonl(path)):
            legacy_id = _text(record, "id") or f"line-{index + 1}"
            if issue_store.legacy_imported(source, legacy_id):
                counts["skipped"] += 1
                continue
            draft = factory(record)
            issue = issue_store.get_by_fingerprint(draft.fingerprint)
            if issue is None:
                issue = issue_store.report(draft)
            issue_store.mark_legacy_imported(source, legacy_id, issue.id)
            counts[source] += 1
    return counts
