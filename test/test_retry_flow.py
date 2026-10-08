"""手动重试通道验收——issue retry 挂账建 job、双击唯一、占位收敛、
失败不误标账、崩溃自愈（recover_stale 由持锁执行者显式调用）。

直接运行:  .venv/bin/python test/test_retry_flow.py
"""

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from helpers import make_issue_store, make_job_service

from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.issues import IssueActionConflict, IssueDraft, IssueKind, IssueStatus
from wiki_agent.jobs import JobResult, PipelineBusy
from wiki_agent.jobs.service import JobService
from wiki_agent.jobs.worker import JobWorker
from wiki_agent.snapshots import digest_file_text
from wiki_agent.sync.source_jobs import SourceJobHandler
from wiki_agent.sync.state import SyncState


def _failure_issue(service: JobService, source: Path):
    return service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title=f"{source.name} 处理失败",
            summary="plan 失败",
            resource={"type": "input_file", "path": source.name, "label": source.name},
            context={"source_path": str(source)},
        )
    )


def test_issue_retry_creates_job(tmp_path: Path):
    """重试路径在 jobs 表新增挂账 compile job；提交不改 issue 状态。"""
    service = make_job_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("重试内容", encoding="utf-8")
    issue = _failure_issue(service, source)

    job = service.submit_issue_retry(issue.id)
    assert job.kind == "compile" and job.mode == "issue_retry"
    assert job.issue_id == issue.id and job.resource == str(source.resolve())
    assert job.payload["digest"] == digest_file_text(source)[0]
    # "在途"由 jobs join 表达——issue 保持 open，无镜像状态
    assert service.issues.get(issue.id).status == IssueStatus.OPEN
    assert service.store.in_flight_job_by_issue(issue.id) is not None


def test_double_retry_click_single_job(tmp_path: Path):
    """双击 retry 收敛为"同一 job、至多一个在途"。"""
    service = make_job_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("重试内容", encoding="utf-8")
    issue = _failure_issue(service, source)
    first = service.submit_issue_retry(issue.id)
    second = service.submit_issue_retry(issue.id)
    assert second.id == first.id
    assert service.store.count_in_flight() == 1


def test_retry_attaches_to_occupant_and_converges(tmp_path: Path):
    """retry 撞他人占位的在途 job：收敛返回占位者并补挂 issue_id。"""
    service = make_job_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    occupant = service.submit(kind="compile", resource=str(source.resolve()), mode="sync")
    assert occupant.issue_id == ""
    issue = _failure_issue(service, source)

    result = service.submit_issue_retry(issue.id)
    assert result.id == occupant.id
    assert service.store.get(occupant.id).issue_id == issue.id
    assert service.store.count_in_flight() == 1


def test_submit_port_rejects_ineligible_retry(tmp_path: Path):
    """资格收口在提交口：终态账与非编译失败账直投也被拒（入口不再自带判定）。"""
    service = make_job_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    issue = _failure_issue(service, source)
    service.issues.transition(issue.id, IssueStatus.RESOLVED)

    try:
        service.submit_issue_retry(issue.id)
        assert False, "终态 issue 不应能重试"
    except IssueActionConflict:
        pass

    quality = service.issues.report(
        IssueDraft(
            kind=IssueKind.QUALITY_ISSUE,
            title="质量问题",
            summary="s",
            resource={"type": "wiki_page", "path": "concepts/a.md"},
        )
    )
    try:
        service.submit_issue_retry(quality.id)
        assert False, "非编译失败账不应能重试"
    except IssueActionConflict:
        pass
    assert service.store.count_in_flight() == 0


def test_retry_batch_enqueues_all_without_self_blocking(tmp_path: Path):
    """批量入队不被本批自产行挡：两个候选两行在途——逐单提交时第二发撞第一发的闸。"""
    service = make_job_service(tmp_path)
    issue_ids = []
    for name in ("a.md", "b.md"):
        source = tmp_path / name
        source.write_text(f"重试输入 {name}" * 10, encoding="utf-8")
        issue_ids.append(_failure_issue(service, source).id)

    jobs = service.submit_issue_retry_batch(issue_ids)
    assert len(jobs) == 2 and len({j.id for j in jobs}) == 2
    assert all(j.kind == "compile" and j.mode == "issue_retry" for j in jobs)
    assert service.store.count_in_flight() == 2


def test_retry_batch_gate_rejects_whole_batch_atomically(tmp_path: Path):
    """批外在途写：整批 PipelineBusy 拒绝，结束后没有半批新行、issue 无一挂账。"""
    service = make_job_service(tmp_path)
    service.submit(kind="compile", resource=str((tmp_path / "other.md").resolve()), mode="sync")
    issue_ids = []
    for name in ("a.md", "b.md"):
        source = tmp_path / name
        source.write_text(f"内容 {name}" * 10, encoding="utf-8")
        issue_ids.append(_failure_issue(service, source).id)

    try:
        service.submit_issue_retry_batch(issue_ids)
        assert False, "有批外在途写时整批应拒"
    except PipelineBusy:
        pass
    assert service.store.count_in_flight() == 1
    for issue_id in issue_ids:
        assert service.store.in_flight_job_by_issue(issue_id) is None


def test_retry_batch_converges_duplicate_issue(tmp_path: Path):
    """批内同一 issue 出现两次：第二发收敛到第一发刚入队的行，不叠行。"""
    service = make_job_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("重试内容" * 10, encoding="utf-8")
    issue_id = _failure_issue(service, source).id

    jobs = service.submit_issue_retry_batch([issue_id, issue_id])
    assert jobs[0].id == jobs[1].id
    assert service.store.count_in_flight() == 1


def test_failed_job_does_not_mark_hash(tmp_path: Path):
    """sync 快照下 ingest 失败：SyncState 无记录；issue 记账等人，不排程。"""
    state = SyncState(tmp_path / "watch" / "state.json")
    service = make_job_service(tmp_path, sync_state=state)
    src = tmp_path / "materials"
    source = src / "note.md"
    source.parent.mkdir(parents=True)
    source.write_text("失败输入" * 20, encoding="utf-8")

    class _FailingPipeline:
        async def ingest_one(self, raw_file):
            raise IngestError(IngestStage.PLAN, "校验失败", source=source.name)

    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    handler = SourceJobHandler(
        _FailingPipeline(),
        state,
        wiki_dir=tmp_path / "wiki",
        source_records_dir=tmp_path / "provenance",
        snapshots=service.snapshots,
    )
    worker = JobWorker(service)
    handler.register_jobs(worker)
    jobs = service.submit_sync(src)
    assert len(jobs) == 1
    asyncio.run(worker.run_once())

    assert service.store.get(jobs[0].id).status == "failed"
    assert state.get(str(source.resolve())).hash == "", "失败不会把内容标为已处理"
    failures = service.issues.list(kinds={IssueKind.INGESTION_FAILURE})
    assert len(failures) == 1
    assert failures[0].retry["policy"] == "manual", "失败就是等人的账"
    assert "next_retry_at" not in failures[0].retry, "无任何自动排程字段"


def test_success_resolves_issue_and_marks_hash(tmp_path: Path):
    """重试成功：job succeeded 与 issue RESOLVED、SyncState 落账同批生效。"""
    state = SyncState(tmp_path / "watch" / "state.json")
    service = make_job_service(tmp_path, sync_state=state)
    source = tmp_path / "note.md"
    source.write_text("重试输入", encoding="utf-8")
    digest, text = digest_file_text(source)
    issue = _failure_issue(service, source)

    service.submit_issue_retry(issue.id)
    job = service.claim_next(kinds={"compile"})
    assert job is not None
    final = service.complete_with_outcome(
        job,
        JobResult(
            status="succeeded",
            detail={"settlement": "ingested", "digest": digest, "text": text},
        ),
    )
    assert final.status == "succeeded"
    assert service.issues.get(issue.id).status == IssueStatus.RESOLVED
    assert state.get(job.resource).hash == digest  # 成功是唯一落账点（先库后文件）


def test_legacy_processing_rows_migrated_to_open(tmp_path: Path):
    """旧库残留：processing 行与 issue_actions 表在 schema v3 启动时一次收敛。"""
    service = make_job_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    issue = _failure_issue(service, source)
    # 模拟旧模型现场：直接写 processing + 建旧账本表
    with service.issues.database.transaction(immediate=True) as conn:
        conn.execute("UPDATE issues SET status = 'processing' WHERE id = ?", (issue.id,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS issue_actions (id TEXT PRIMARY KEY, issue_id TEXT)"
        )
        conn.execute("INSERT INTO issue_actions VALUES ('a1', ?)", (issue.id,))

    make_issue_store(tmp_path)  # 重新初始化触发 v3 迁移
    assert service.issues.get(issue.id).status == IssueStatus.OPEN
    with service.issues.database.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='issue_actions'"
        ).fetchone()
    assert row is None


def test_recover_stale_is_explicit_post_lock(tmp_path: Path):
    """崩溃自愈：卡死的 running 行由下一个执行者**持锁后显式**回队。

    构造 service 不再自动回收——构造不代表持有执行锁，未持锁就回队
    会与活进程双写 wiki（CAS 保证终态一写，不保证写入者唯一）。
    """
    service = make_job_service(tmp_path)
    job = service.submit(kind="compile", resource="/stale", mode="sync")
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    stale_time = (datetime.now(UTC) - timedelta(seconds=900)).isoformat()
    with service.store.database.transaction(immediate=True) as conn:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (stale_time, job.id))

    revived = make_job_service(tmp_path)
    assert revived.recovered_jobs == 0  # 构造期不再回收
    assert revived.store.get(job.id).status == "running"
    assert revived.recover_stale() == 1  # 持锁后的显式调用才回收
    assert revived.store.get(job.id).status == "queued"


def test_final_result_persists_bounded_facts_only(tmp_path: Path):
    """终态落账只留有界事实：正文级 detail 键供 outcomes 进程内消费，
    不进 jobs 行——否则 state.db 随每轮 sync 无界膨胀、list() 反复反序列化大 blob。
    """
    from wiki_agent.jobs import JobResult, Kind

    service = make_job_service(tmp_path)
    service.submit(kind=Kind.COMPILE, resource="/doc.md", mode="sync")
    claimed = service.claim_next(kinds={Kind.COMPILE})
    assert claimed is not None
    done = service.complete_with_outcome(
        claimed,
        JobResult(
            status="succeeded",
            detail={
                "settlement": "ingested",
                "digest": "abc123",
                "text": "页面全文" * 500,
                "source_page": {"slug": "sources/doc", "content": "档案全文" * 500},
            },
        ),
    )
    assert done.result == {"settlement": "ingested", "digest": "abc123"}


if __name__ == "__main__":
    import traceback

    failed = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t(Path(tempfile.mkdtemp()))
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)

