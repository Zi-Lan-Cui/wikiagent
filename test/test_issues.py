"""Unified issue models, persistence, projection tests."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from wiki_agent.application.issue_actions import IssueActionExecutor, resolve_correction_issue
from wiki_agent.application.issue_tasks import IssueTaskManager
from wiki_agent.application.runtime import AppRuntime
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


def test_issue_store_claim_is_atomic_and_audited(tmp_path: Path):
    store = IssueStore(tmp_path)
    issue = store.report(_draft())
    action_id = store.claim_action(issue.id, "retry")

    with pytest.raises(IssueAlreadyClaimedError):
        store.claim_action(issue.id, "retry")

    resolved = store.complete_action(
        action_id, status=IssueStatus.RESOLVED, result={"run_id": "r1"}
    )
    assert resolved.status == IssueStatus.RESOLVED
    assert resolved.resolution == {"run_id": "r1"}
    assert store.events(issue.id)[-1]["event"] == "action_completed"


def test_issue_store_recovers_interrupted_action(tmp_path: Path):
    store = IssueStore(tmp_path)
    issue = store.report(_draft())
    store.claim_action(issue.id, "retry")

    recovered = IssueStore(tmp_path).recover_interrupted_actions()

    assert recovered == 1
    assert store.require(issue.id).status == IssueStatus.BLOCKED
    assert store.events(issue.id)[-1]["event"] == "action_interrupted"


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


def test_failed_retry_updates_issue_with_structured_page_reason(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    source = tmp_path / "note.md"
    source.write_text("# note", encoding="utf-8")
    store = IssueStore(workspace)
    service = IssueService(store)
    issue = service.report(
        _draft(
            origin={"mode": "compile"},
            resource={"type": "input_file", "path": source.name},
            context={"source_path": str(source)},
        )
    )
    runtime = SimpleNamespace(
        workspace=workspace,
        wiki_dir=wiki,
        source_records_dir=workspace / "provenance" / "sources",
        runs_dir=workspace / "runs",
        issue_store=store,
        issue_service=service,
        agent=SimpleNamespace(llm=object(), vlm=None),
        config=SimpleNamespace(compile=object(), retry=object()),
    )

    async def failed_retry(*_args, **_kwargs):
        return {
            "committed": False,
            "message": "1 个页面生成失败",
            "diagnostics": {
                "detail": "1 个页面生成失败",
                "stage": "execute",
                "error_code": "page_generation_failed",
                "failures": [{"path": "concepts/example.md", "reason": "frontmatter 缺少 title"}],
            },
            "results": [{"id": issue.id, "status": "failed", "attempts": 2}],
        }

    monkeypatch.setattr("wiki_agent.application.issue_actions.retry_source_failures", failed_retry)

    with pytest.raises(RuntimeError, match="1 个页面生成失败"):
        asyncio.run(IssueActionExecutor(cast(AppRuntime, runtime))._retry_ingestion(issue.id))

    updated = store.require(issue.id)
    assert updated.diagnostics["stage"] == "execute"
    assert updated.diagnostics["error_code"] == "page_generation_failed"
    assert updated.diagnostics["failures"] == [
        {"path": "concepts/example.md", "reason": "frontmatter 缺少 title"}
    ]
    assert updated.retry["attempts"] == 2


def test_issue_task_manager_returns_trackable_result(tmp_path: Path):
    service = IssueService(IssueStore(tmp_path))
    issue = service.report(_draft())

    class _Executor:
        store = service.store

        async def execute(self, issue_id, action, payload=None, *, progress=None):
            assert issue_id == issue.id
            assert action == "retry"
            if progress is not None:
                progress("analyze")
            return service.get(issue_id)

    async def run():
        manager = IssueTaskManager(_Executor())
        task = manager.start(issue.id, "retry")
        for _ in range(10):
            await asyncio.sleep(0)
            current = manager.get(task.id)
            if current.status == "completed":
                break
        assert current.status == "completed"
        assert current.resource == "notes/note.md"
        assert current.source_stage == "plan"
        assert current.current_stage == "已完成"
        assert current.stage_code == "analyze"
        assert current.stage_index == 6
        assert current.stage_total == 10
        assert current.result is not None
        assert manager.list() == [current]
        await manager.close()

    asyncio.run(run())


def test_issue_task_manager_serializes_wiki_actions(tmp_path: Path):
    service = IssueService(IssueStore(tmp_path))
    first = service.report(_draft(fingerprint="task-first"))
    second = service.report(_draft(fingerprint="task-second", title="second.md 处理失败"))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    running = 0
    maximum_running = 0

    class _Executor:
        store = service.store

        async def execute(self, issue_id, action, payload=None, *, progress=None):
            nonlocal running, maximum_running
            running += 1
            maximum_running = max(maximum_running, running)
            try:
                if issue_id == first.id:
                    first_started.set()
                    await release_first.wait()
                return service.get(issue_id)
            finally:
                running -= 1

    async def run():
        manager = IssueTaskManager(_Executor())
        first_task = manager.start(first.id, "retry")
        second_task = manager.start(second.id, "retry")
        await first_started.wait()
        assert manager.active_issue_ids() == {first.id, second.id}
        assert manager.get(first_task.id).status == "running"
        assert manager.get(second_task.id).status == "queued"
        release_first.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if manager.get(second_task.id).status == "completed":
                break
        assert maximum_running == 1
        assert manager.get(second_task.id).status == "completed"
        assert manager.active_issue_ids() == set()
        await manager.close()

    asyncio.run(run())
