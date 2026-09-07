"""Source failure reporting and retry policy tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wiki_agent.compiler.workflows.failures import SourceFailureConsumer, SourceFailureHandler
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


def test_consumer_succeeds_and_persists_attempt(tmp_path: Path):
    store, issue = _reported_failure(tmp_path)
    called = False

    async def process(_issue):
        nonlocal called
        called = True

    result = asyncio.run(SourceFailureConsumer(store, process).consume(issue, force=True))
    assert result["status"] == "succeeded"
    assert called
    assert store.require(issue.id).retry["attempts"] == 2


def test_consumer_defers_and_backoffs_in_issue_store(tmp_path: Path):
    store, issue = _reported_failure(tmp_path)
    future = (datetime.now() + timedelta(hours=1)).isoformat()
    store.update_payloads(issue.id, retry={**issue.retry, "next_retry_at": future})
    issue = store.require(issue.id)

    async def process(_issue):
        raise RuntimeError("still unavailable")

    deferred = asyncio.run(SourceFailureConsumer(store, process).consume(issue))
    assert deferred["status"] == "deferred"

    failed = asyncio.run(
        SourceFailureConsumer(
            store,
            process,
            base_delay_seconds=10,
            max_delay_seconds=100,
        ).consume(issue, force=True)
    )
    updated = store.require(issue.id)
    assert failed["status"] == "failed"
    assert updated.retry["attempts"] == 2
    assert updated.retry["next_retry_at"]


def test_consumer_preserves_page_reasons_without_raw_output(tmp_path: Path):
    store, issue = _reported_failure(tmp_path)

    async def process(_issue):
        raise IngestError(
            IngestStage.EXECUTE,
            "1 个页面生成失败",
            raw=json.dumps(
                [{"path": "concepts/example.md", "error": "缺少 title", "raw": "private"}]
            ),
            error_code="page_generation_failed",
            error_class="transient",
            retry_policy="auto_retry",
        )

    result = asyncio.run(SourceFailureConsumer(store, process).consume(issue, force=True))
    assert result["diagnostics"]["failures"] == [
        {"path": "concepts/example.md", "reason": "缺少 title"}
    ]
    assert "private" not in json.dumps(store.require(issue.id).diagnostics, ensure_ascii=False)


def test_expired_issue_becomes_blocked(tmp_path: Path):
    store, issue = _reported_failure(tmp_path)
    store.update_payloads(
        issue.id,
        retry={**issue.retry, "expires_at": (datetime.now() - timedelta(seconds=1)).isoformat()},
    )
    result = asyncio.run(
        SourceFailureConsumer(store, lambda _: None).consume(store.require(issue.id))
    )
    assert result["status"] == "manual"
    assert store.require(issue.id).status == IssueStatus.BLOCKED


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
