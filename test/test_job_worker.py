from __future__ import annotations

import asyncio

from wiki_agent.application.job_service import JobService
from wiki_agent.application.job_worker import JobWorker


def test_worker_claims_updates_stage_and_completes(tmp_path):
    service = JobService(tmp_path)
    job = service.submit(kind="compile", resource="note.md", mode="sync")
    worker = JobWorker(service)
    stages: list[str] = []

    async def handle(current, progress):
        progress("extract")
        stages.append(current.resource)

    worker.register("compile", handle)
    asyncio.run(worker.run_once())

    result = service.store.get(job.id)
    assert result.status == "succeeded"
    assert result.stage == "completed"
    assert stages == ["note.md"]


def test_worker_with_no_registrations_claims_nothing(tmp_path):
    """S1 契约：零注册 worker 不领任何活——排队行原样等待有资格的进程。"""
    service = JobService(tmp_path)
    job = service.submit(kind="unknown", resource="note.md", mode="test")
    assert asyncio.run(JobWorker(service).run_once()) is None
    assert service.store.get(job.id).status == "queued"


# 终态写入与 outcome 联动


class _FakeSyncState:
    def __init__(self):
        self.records: list = []
        self.drops: list = []
        self.saved = 0

    def record(self, path, digest, text):
        self.records.append((path, digest, text))

    def drop(self, path):
        self.drops.append(path)

    def save(self):
        self.saved += 1


def _service_with_state(tmp_path):
    from wiki_agent.application.job_service import JobService as _JS

    state = _FakeSyncState()
    service = _JS(tmp_path, sync_state=state)
    return service, state


def test_transient_failure_reports_issue_without_chain(tmp_path):
    """handler 抛未预期异常 → failed 终态 + run_failure issue；无后继排程。"""
    from wiki_agent.issues import IssueKind, IssueStore

    service = JobService(tmp_path)
    job = service.submit(kind="compile", resource="/x", mode="sync")
    worker = JobWorker(service)

    async def crash(current, progress):
        raise RuntimeError("boom")

    worker.register("compile", crash)
    asyncio.run(worker.run_once())

    assert service.store.get(job.id).status == "failed"
    assert service.store.in_flight_by_resource("/x") is None, "手动模型不排链"
    issues = IssueStore(tmp_path).list(kinds={IssueKind.RUN_FAILURE})
    assert len(issues) == 1
    assert "boom" in issues[0].summary


def test_succeeded_writes_sync_state_after_commit(tmp_path):
    """成功 outcome：commit 后 record(digest/text)；delete 成功 drop+save。"""
    service, state = _service_with_state(tmp_path)
    service.submit(kind="compile", resource="/src/a.md", mode="sync")
    worker = JobWorker(service)

    async def ok(current, progress):
        from wiki_agent.application.job_results import JobResult

        return JobResult(status="succeeded", detail={"digest": "d1", "text": "content-a"})

    worker.register("compile", ok)
    asyncio.run(worker.run_once())
    assert state.records == [("/src/a.md", "d1", "content-a")]

    service.submit(kind="delete", resource="/src/b.md", mode="sync")
    worker2 = JobWorker(service)
    worker2.register("delete", ok)
    asyncio.run(worker2.run_once())
    assert state.drops == ["/src/b.md"] and state.saved == 1


def test_cancelled_leaves_linked_issue_open(tmp_path):
    """cancel_terminal：挂账 job 被取消 → job cancelled；issue 全程 open 无账可还。"""
    from wiki_agent.issues import IssueDraft, IssueKind, IssueStatus

    service = JobService(tmp_path)
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="t",
            summary="s",
            resource={"type": "input_file", "path": "note.md", "label": "note.md"},
            context={"source_path": "/abs/note.md"},
        )
    )
    job = service.submit(
        kind="compile", resource="/abs/note.md", mode="issue_retry", issue_id=issue.id
    )
    service.store.update(job.id, status="running")
    service.cancel_terminal(job)
    assert service.store.get(job.id).status == "cancelled"
    assert service.issues.get(issue.id).status == IssueStatus.OPEN
    # 取消不产生任何 issue 事件——语义是 no-op
    assert service.issues.events(issue.id)[-1]["event"] != "job_cancelled"


def test_worker_claims_only_registered_kinds(tmp_path):
    service = JobService(tmp_path)
    service.submit(kind="compile", resource="/mine", mode="sync")
    other = service.submit(kind="issue_action", resource="i1", mode="retry")
    worker = JobWorker(service)

    async def ok(current, progress):
        pass

    worker.register("compile", ok)
    asyncio.run(worker.run_once())
    assert service.store.get(other.id).status == "queued"  # 不越界领取别人类型


def test_terminal_cas_supersede_race(tmp_path):
    """取代竞态：行已被 cancel → 迟到终态写静默跳过，不排链、不覆盖。"""
    from wiki_agent.application.job_results import JobResult

    service = JobService(tmp_path)
    job = service.submit(kind="compile", resource="/abs/note.md", mode="sync")
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None and claimed.id == job.id

    # 前驱把行取消在前（模拟取代/停机竞态的直写）
    service.store.update(job.id, status="cancelled", stage="cancelled")

    final = service.complete_with_outcome(
        claimed,
        JobResult(status="failed", error_type="transient", detail={"error": "boom"}),
    )
    assert final.status == "cancelled"
    # transient 链不产生——没有新的在途行
    assert service.store.count_in_flight() == 0

    # 迟到的 cancel_terminal 同样幂等静默
    service.cancel_terminal(claimed)
    assert service.store.get(job.id).status == "cancelled"
    assert service.store.count_in_flight() == 0
