"""Source failure 记账测试（手动重试模型版）。

handler 负责 compile/refine 内联上报；失败只记账不排程——retry 快照
（attempts/last_error/policy=manual）由 IssueStore.report_failure 统一合成；
"执行→回写"联动在 JobOutcomeHandler（见 test_retry_flow）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wiki_agent.application.job_results import JobResult
from wiki_agent.application.job_service import JobService
from wiki_agent.compiler.workflows.failures import SourceFailureHandler
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
    assert issue.retry["policy"] == "manual", "手动模型：策略字段恒为 manual"
    assert "next_retry_at" not in issue.retry and "expires_at" not in issue.retry, "无排程字段"
    assert not (tmp_path / "queue.jsonl").exists()


def test_failure_handler_classifies_error_in_diagnostics(tmp_path: Path):
    """error_class 只进诊断展示——不再决定任何重试通道。"""
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
    assert issue.retry["policy"] == "manual"


# retry 快照（同一指纹重复失败：attempts 递增、人注标记保留）


def test_report_failure_bumps_attempts_by_fingerprint(tmp_path: Path):
    store = IssueStore(tmp_path)

    def draft(error: str) -> IssueDraft:
        return IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="note.md 处理失败",
            summary=error,
            resource={"type": "input_file", "path": "note.md", "label": "note.md"},
            context={"source_path": "/abs/note.md"},
        )

    first = store.report_failure(draft("第一次错"), "第一次错")
    store.update_payloads(first.id, retry={**first.retry, "unavailable_reason": "来源不稳"})
    second = store.report_failure(draft("第二次错"), "第二次错")

    assert second.id == first.id, "同指纹合并入账，occurrences 递增"
    assert store.require(first.id).occurrences == 2
    record = store.require(first.id)
    assert record.retry["attempts"] == 2
    assert record.retry["last_error"] == "第二次错"
    assert record.retry["unavailable_reason"] == "来源不稳", "人注标记跨失败保留"


# 失败联动（outcome）——只记账，不排程，状态停 open


def _ingest_error_result(source: Path, error: str) -> JobResult:
    return JobResult(
        status="failed",
        error_type="ingest_error",
        detail={
            "error": error,
            "stage": "plan",
            "source": source.name,
            "source_path": str(source),
            "diagnostics": {"error_code": "ingest_error"},
        },
    )


def test_ingest_error_merges_and_keeps_issue_open(tmp_path: Path):
    """同一失败重复发生：outcome 按指纹合并进同一账——attempts 递增、停 open、无排程。"""
    source = tmp_path / "note.md"
    source.write_text("重试输入", encoding="utf-8")
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    service.submit(kind="compile", resource=str(source.resolve()), mode="sync")
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    service.complete_with_outcome(claimed, _ingest_error_result(source, "第一次失败"))

    failures = service.issues.list(kinds={IssueKind.INGESTION_FAILURE})
    assert len(failures) == 1
    issue_id = failures[0].id
    assert failures[0].status == IssueStatus.OPEN

    # 人工 retry → 再失败：合并同账，不另开
    service.submit_issue_retry(issue_id)
    again = service.claim_next(kinds={"compile"})
    assert again is not None
    service.complete_with_outcome(again, _ingest_error_result(source, "第二次失败"))

    merged = service.issues.get(issue_id)
    assert service.issues.list(kinds={IssueKind.INGESTION_FAILURE}).__len__() == 1
    assert merged.status == IssueStatus.OPEN, "失败停 open 等人——无退避、无耗尽转 BLOCKED"
    assert merged.retry["attempts"] == 2
    assert merged.retry["last_error"] == "第二次失败"
    assert "next_retry_at" not in merged.retry


def test_transient_failure_reports_run_failure_issue(tmp_path: Path):
    """未预期异常 = run_failure 一笔账，不再有链式重试排程。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    job = service.submit(kind="compile", resource="/abs/x.md", mode="sync", payload={"digest": "d"})
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    service.complete_with_outcome(
        claimed,
        JobResult(status="failed", error_type="transient", detail={"error": "KeyError: boom"}),
    )
    failures = service.issues.list(kinds={IssueKind.RUN_FAILURE})
    assert len(failures) == 1
    assert "boom" in failures[0].summary
    assert service.store.count_in_flight() == 0, "transient 不产生后继 job"


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
