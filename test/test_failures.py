"""Source failure reporting 与重试策略测试（统一 Job 模型版）。

handler 负责 compile/refine 内联上报；重试策略是纯函数；
"执行→回写"联动在 JobOutcomeHandler（见 test_retry_flow）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wiki_agent.application.job_results import JobResult
from wiki_agent.application.job_service import JobService
from wiki_agent.compiler.workflows.failures import (
    SourceFailureHandler,
    is_retry_due,
    source_backoff_seconds,
    source_retry_decision,
)
from wiki_agent.compiler.workflows.retry import SourceUnavailableError, resolve_retry_source
from wiki_agent.errors import IngestError, IngestStage, RetryableError
from wiki_agent.issues import IssueDraft, IssueKind, IssueService, IssueStatus, IssueStore


def _reported_failure(tmp_path: Path, *, error: IngestError | None = None):
    store = IssueStore(tmp_path)
    handler = SourceFailureHandler(IssueService(store), mode="compile")
    handler.handle(
        error
        or IngestError(
            IngestStage.PLAN,
            "plan 输出校验失败",
            source="note.md",
            raw='{"bad": true}',
            error_class="transient",
            retry_policy="auto_retry",
        ),
        source="note.md",
        source_path=tmp_path / "note.md",
    )
    return store, store.list()[0]


def test_source_failure_handler_writes_only_issue_store(tmp_path: Path):
    store, issue = _reported_failure(tmp_path)

    assert issue.kind == IssueKind.INGESTION_FAILURE
    assert issue.origin == {"mode": "compile", "reported_by": "compile", "stage": "plan"}
    assert issue.resource["path"] == "note.md"
    assert issue.diagnostics["error_code"] == "ingest_error"
    assert issue.retry["attempts"] == 1
    assert not (tmp_path / "queue.jsonl").exists()


def test_failure_handler_classifies_retryable_error(tmp_path: Path):
    _, issue = _reported_failure(
        tmp_path,
        error=IngestError(
            IngestStage.EXTRACT,
            "timeout",
            source="note.md",
            cause=RetryableError("timeout"),
        ),
    )
    assert issue.diagnostics["error_class"] == "transient"
    assert issue.retry["policy"] == "auto_retry"


# 策略纯函数


def test_retry_decision_transitions():
    now = datetime.now(UTC)
    base = {"policy": "auto_retry", "attempts": 1, "expires_at": ""}
    assert source_retry_decision(base, max_attempts=3, now=now) == "retry"
    assert source_retry_decision({**base, "attempts": 3}, max_attempts=3, now=now) == "manual"
    once = {"policy": "retry_once", "attempts": 1}
    assert source_retry_decision(once, max_attempts=3, now=now) == "retry"
    assert source_retry_decision({**once, "attempts": 2}, max_attempts=3, now=now) == "manual"
    expired = {**base, "expires_at": (now - timedelta(seconds=1)).isoformat()}
    assert source_retry_decision(expired, max_attempts=3, now=now) == "manual"
    assert source_retry_decision({"policy": "manual", "attempts": 1}, max_attempts=3) == "manual"


def test_retry_due_and_backoff():
    now = datetime.now(UTC)
    assert is_retry_due({}, now=now) is True
    assert is_retry_due({"next_retry_at": (now + timedelta(hours=1)).isoformat()}, now=now) is False
    assert (
        is_retry_due({"next_retry_at": (now - timedelta(seconds=1)).isoformat()}, now=now) is True
    )
    assert source_backoff_seconds(1, base=10, max_delay=100) == 10
    assert source_backoff_seconds(3, base=10, max_delay=100) == 40
    assert source_backoff_seconds(9, base=10, max_delay=100) == 100


def test_failure_diagnostics_sanitizes_raw():
    """页级失败诊断只保留 path/reason——raw 大段输出不得进 issue。"""
    from wiki_agent.compiler.workflows.failures import failure_diagnostics

    exc = IngestError(
        IngestStage.EXECUTE,
        "1 个页面生成失败",
        raw=json.dumps([{"path": "concepts/example.md", "error": "缺少 title", "raw": "private"}]),
        error_code="page_generation_failed",
        error_class="transient",
        retry_policy="auto_retry",
    )
    diagnostics, raw = failure_diagnostics(exc)
    assert diagnostics["failures"] == [{"path": "concepts/example.md", "reason": "缺少 title"}]
    assert "private" not in json.dumps(diagnostics, ensure_ascii=False)
    assert "private" in raw  # raw 只进事件日志


# 重试链联动（outcome）


def test_retry_job_failure_advances_backoff_and_returns_open(tmp_path: Path):
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("重试输入", encoding="utf-8")
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="note.md 处理失败",
            summary="plan 失败",
            resource={"type": "input_file", "path": "note.md", "label": "note.md"},
            context={"source_path": str(source)},
            retry={"policy": "auto_retry", "attempts": 1, "next_retry_at": ""},
        )
    )
    service.submit_issue_retry(issue.id)
    assert service.issues.get(issue.id).status == IssueStatus.OPEN
    assert service.store.has_active_job_by_issue(issue.id)
    job = service.claim_next(kinds={"compile"})
    assert job is not None

    service.complete_with_outcome(
        job,
        JobResult(
            status="failed",
            error_type="ingest_error",
            detail={
                "error": "again failed",
                "stage": "plan",
                "source": "note.md",
                "source_path": str(source),
                "diagnostics": {"error_code": "ingest_error"},
            },
        ),
    )
    updated = service.issues.get(issue.id)
    assert updated.status == IssueStatus.OPEN  # 归还给调度器继续退避
    assert updated.retry["attempts"] == 2
    assert updated.retry["next_retry_at"]  # 退避在后


def test_retry_job_exhausted_blocks(tmp_path: Path):
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("重试输入", encoding="utf-8")
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="note.md 处理失败",
            summary="plan 失败",
            resource={"type": "input_file", "path": "note.md", "label": "note.md"},
            context={"source_path": str(source)},
            retry={"policy": "auto_retry", "attempts": 3, "next_retry_at": ""},
        )
    )
    service.submit_issue_retry(issue.id)
    job = service.claim_next(kinds={"compile"})
    assert job is not None
    service.complete_with_outcome(
        job,
        JobResult(
            status="failed",
            error_type="ingest_error",
            detail={
                "error": "final failure",
                "stage": "plan",
                "source": "note.md",
                "source_path": str(source),
                "diagnostics": {"error_code": "ingest_error"},
            },
        ),
    )
    final = service.issues.get(issue.id)
    assert final.status == IssueStatus.BLOCKED


# 重试输入解析


def test_retry_resolves_stale_refine_path_from_current_wiki(tmp_path: Path):
    wiki = tmp_path / "wiki"
    page = wiki / "concepts" / "move-semantics.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Move semantics", encoding="utf-8")
    store = IssueStore(tmp_path)
    issue = store.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="move-semantics.md 处理失败",
            summary="临时失败",
            origin={"mode": "refine"},
            resource={"type": "wiki_page", "path": "move-semantics.md"},
            context={"source_path": "/tmp/deleted/concepts/move-semantics.md"},
        )
    )
    assert resolve_retry_source(store.require(issue.id), wiki) == page.resolve()


def test_retry_rejects_missing_original_compile_source(tmp_path: Path):
    _, issue = _reported_failure(tmp_path)
    with pytest.raises(SourceUnavailableError, match="原始来源已不存在"):
        resolve_retry_source(issue, tmp_path / "wiki")
