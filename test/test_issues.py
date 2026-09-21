"""Unified issue models, persistence, projection tests."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wiki_agent.application.issue_actions import resolve_correction_issue
from wiki_agent.events import RunContext
from wiki_agent.issues import (
    IssueAlreadyClaimedError,
    IssueDraft,
    IssueKind,
    IssueService,
    IssueSeverity,
    IssueStatus,
    IssueStore,
)
from wiki_agent.issues.hooks import IssueReporterHook
from wiki_agent.issues.producers import report_correction


def _draft(**changes) -> IssueDraft:
    values = {
        "kind": IssueKind.INGESTION_FAILURE,
        "title": "note.md 处理失败",
        "summary": "模型输出校验失败",
        "severity": IssueSeverity.ERROR,
        "origin": {"mode": "compile", "stage": "plan"},
        "resource": {"type": "input_file", "path": "notes/note.md"},
        "diagnostics": {"error_code": "output_validation"},
        "retry": {"policy": "auto_retry", "attempts": 1},
        "evidence": [{"key": "note.md:plan"}],
    }
    values.update(changes)
    return IssueDraft(**values)


def test_issue_store_reports_and_deduplicates(tmp_path: Path):
    store = IssueStore(tmp_path)
    first = store.report(_draft())
    second = store.report(_draft(summary="同一问题再次出现"))

    assert first.id == second.id
    assert second.occurrences == 2
    assert second.summary == "同一问题再次出现"
    assert [event["event"] for event in store.events(first.id)] == ["reported", "reoccurred"]


def test_issue_store_filters_and_transitions(tmp_path: Path):
    store = IssueStore(tmp_path)
    ingest = store.report(_draft())
    store.report(
        _draft(
            kind=IssueKind.CONTENT_CORRECTION,
            title="页面纠错",
            fingerprint="correction-1",
            severity=IssueSeverity.WARNING,
        )
    )
    dismissed = store.transition(ingest.id, IssueStatus.DISMISSED, resolution={"reason": "noise"})
    assert dismissed.status == IssueStatus.DISMISSED
    assert dismissed.resolution == {"reason": "noise"}
    assert [item.id for item in store.list(statuses={IssueStatus.DISMISSED})] == [ingest.id]
    assert len(store.list(kinds={IssueKind.CONTENT_CORRECTION})) == 1
    assert store.count(statuses={IssueStatus.DISMISSED}) == 1


def test_transition_cas_guards_expected_state(tmp_path: Path):
    """expected CAS 取代旧 claim 原子性：状态不符的操作方大声失败。"""
    store = IssueStore(tmp_path)
    issue = store.report(_draft())

    store.transition(
        issue.id, IssueStatus.RESOLVED, expected={IssueStatus.OPEN}, resolution={"run_id": "r1"}
    )
    assert store.require(issue.id).status == IssueStatus.RESOLVED
    # 二次操作（如迟到的裁决）被 CAS 挡下——账本只有一份真相
    with pytest.raises(IssueAlreadyClaimedError):
        store.transition(issue.id, IssueStatus.OPEN, expected={IssueStatus.OPEN})


def test_issue_card_hides_absolute_paths_and_derives_retry_state(tmp_path: Path):
    store = IssueStore(tmp_path)
    expired = (datetime.now() - timedelta(seconds=1)).isoformat()
    issue = store.report(
        _draft(
            resource={"type": "input_file", "path": "/home/user/private/note.md"},
            diagnostics={"error_code": "timeout", "log": "/tmp/secret/run.log"},
            retry={"policy": "auto_retry", "attempts": 1, "expires_at": expired},
        )
    )
    card = IssueService(store).get(issue.id)
    payload = asdict(card)

    assert card.attention == "retryable"
    assert card.resource["path"] == "note.md"
    assert card.diagnostics["log"] == "run.log"
    assert [action.id for action in card.available_actions][:2] == [
        "retry",
        "open_resource",
    ]
    assert "context" not in payload


def test_unavailable_source_disables_retry_and_resource_actions(tmp_path: Path):
    store = IssueStore(tmp_path)
    reason = "原始来源已不存在"
    issue = store.report(_draft(retry={"policy": "auto_retry", "unavailable_reason": reason}))

    card = IssueService(store).get(issue.id)
    actions = {action.id: action for action in card.available_actions}

    assert card.attention == "source_unavailable"
    assert actions["retry"].label == "无法重试"
    assert actions["retry"].disabled_reason == reason
    assert actions["open_resource"].disabled_reason == reason


def test_run_error_hook_reports_fatal_turn_but_not_cancellation(tmp_path: Path):
    service = IssueService(IssueStore(tmp_path))
    hook = IssueReporterHook(service)
    failed = RunContext(session_key="session_1", run_id="run_1")
    failed.error = "provider unavailable"
    failed.exception = RuntimeError("provider unavailable")
    asyncio.run(hook.on_run_error(failed))

    cancelled = RunContext(session_key="session_1", run_id="run_2")
    cancelled.exception = asyncio.CancelledError()
    asyncio.run(hook.on_run_error(cancelled))

    records = service.store.list()
    assert len(records) == 1
    assert records[0].kind == IssueKind.RUN_FAILURE
    assert records[0].origin["run_id"] == "run_1"


def test_correction_decision_uses_audited_issue_workflow(tmp_path: Path):
    service = IssueService(IssueStore(tmp_path))
    issue_id = report_correction(service, text="示例已经过时", page="concepts/example.md")

    card = resolve_correction_issue(service, issue_id, "accept")

    assert card.status == IssueStatus.RESOLVED.value
    follow_up = service.store.get_by_fingerprint(f"accepted-correction:{issue_id}")
    assert follow_up is not None
    assert follow_up.kind == IssueKind.QUALITY_ISSUE


def test_content_conflict_only_advertises_implemented_actions(tmp_path: Path):
    service = IssueService(IssueStore(tmp_path))
    card = service.report(
        _draft(
            kind=IssueKind.CONTENT_CONFLICT,
            fingerprint="conflict-1",
            resource={"type": "wiki_page", "path": "concepts/example.md"},
        )
    )

    actions = {action.id for action in card.available_actions}
    assert "resolve_conflict" not in actions
    assert actions == {"keep_disputed", "open_resource", "dismiss"}


def test_failed_retry_updates_issue_with_structured_page_reason(tmp_path: Path):
    """统一执行模型：重试失败经 Job 结果回写——结构化页级诊断合并进同一 issue。

    handler 上报（指纹 stage+error_code+resource）→ submit_issue_retry 挂账
    → 再失败时 outcome 按同指纹合并（occurrences+1、diagnostics 刷新），
    同时推进该 issue 的退避计数。
    """
    import json

    from wiki_agent.application.job_results import JobResult
    from wiki_agent.application.job_service import JobService
    from wiki_agent.compiler.workflows.failures import SourceFailureHandler
    from wiki_agent.errors import IngestError, IngestStage

    workspace = tmp_path / "workspace"
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    source = tmp_path / "note.md"
    source.write_text("# note", encoding="utf-8")

    service = JobService(workspace, wiki_dir=wiki)
    handler = SourceFailureHandler(service.issue_service, mode="compile")
    err = IngestError(
        IngestStage.EXECUTE,
        "1 个页面生成失败",
        source=source.name,
        raw=json.dumps(
            [{"path": "concepts/example.md", "error": "frontmatter 缺少 title", "raw": "私有输出"}]
        ),
        error_code="page_generation_failed",
        retry_policy="auto_retry",
    )
    handler.handle(err, source=source.name, source_path=source)
    issue = service.issues.list()[0]

    service.submit_issue_retry(issue.id)
    assert service.issues.get(issue.id).status == IssueStatus.OPEN
    assert service.store.has_in_flight_job_by_issue(issue.id)
    job = service.claim_next(kinds={"compile"})
    assert job is not None

    service.complete_with_outcome(
        job,
        JobResult(
            status="failed",
            error_type="ingest_error",
            detail={
                "error": "1 个页面生成失败",
                "stage": "execute",
                "source": source.name,
                "source_path": str(source),
                "retry_policy": "auto_retry",
                "diagnostics": {
                    "stage": "execute",
                    "error_code": "page_generation_failed",
                    "error_class": "transient",
                    "failures": [
                        {"path": "concepts/example.md", "reason": "frontmatter 缺少 title"}
                    ],
                },
            },
        ),
    )

    updated = service.issues.require(issue.id)
    assert updated.occurrences == 2, "同指纹合并进同一问题"
    assert updated.diagnostics["stage"] == "execute"
    assert updated.diagnostics["error_code"] == "page_generation_failed"
    assert updated.diagnostics["failures"] == [
        {"path": "concepts/example.md", "reason": "frontmatter 缺少 title"}
    ]
    assert updated.retry["attempts"] == 2
    assert updated.status == IssueStatus.OPEN, "未耗尽回 open 继续退避"
