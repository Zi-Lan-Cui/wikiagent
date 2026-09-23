"""手动重试通道验收——issue retry 挂账建 job、双击唯一、占位收敛、
失败不误标账、崩溃自愈（recover_stale 于服务构造期）。

直接运行:  .venv/bin/python test/test_retry_flow.py
"""

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.issues import IssueDraft, IssueKind, IssueStatus, IssueStore
from wiki_agent.jobs import JobResult
from wiki_agent.jobs.service import JobService
from wiki_agent.jobs.worker import JobWorker
from wiki_agent.sync.job_consumer import SyncConsumer
from wiki_agent.sync.state import SyncState, digest_file_text


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
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("重试内容", encoding="utf-8")
    issue = _failure_issue(service, source)

    job = service.submit_issue_retry(issue.id)
    assert job.kind == "compile" and job.mode == "issue_retry"
    assert job.issue_id == issue.id and job.resource == str(source.resolve())
    assert job.payload["digest"] == digest_file_text(source)[0]
    # "在途"由 jobs join 表达——issue 保持 open，无镜像状态
    assert service.issues.get(issue.id).status == IssueStatus.OPEN
    assert service.store.has_in_flight_job_by_issue(issue.id)


def test_double_retry_click_single_job(tmp_path: Path):
    """双击 retry 的等价性证明：提交点收敛为"同一 job、至多一个在途"。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("重试内容", encoding="utf-8")
    issue = _failure_issue(service, source)
    first = service.submit_issue_retry(issue.id)
    second = service.submit_issue_retry(issue.id)
    assert second.id == first.id
    assert service.store.count_in_flight() == 1


def test_retry_attaches_to_occupant_and_converges(tmp_path: Path):
    """retry 撞他人占位的在途 job：收敛返回占位者并补挂 issue_id。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    occupant = service.submit(kind="compile", resource=str(source.resolve()), mode="sync")
    assert occupant.issue_id == ""
    issue = _failure_issue(service, source)

    result = service.submit_issue_retry(issue.id)
    assert result.id == occupant.id
    assert service.store.get(occupant.id).issue_id == issue.id
    assert service.store.count_in_flight() == 1


def test_failed_job_does_not_mark_hash(tmp_path: Path):
    """sync 快照下 ingest 失败：SyncState 无记录；issue 记账等人，不排程。"""
    state = SyncState(tmp_path / "watch" / "state.json")
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki", sync_state=state)
    src = tmp_path / "materials"
    source = src / "note.md"
    source.parent.mkdir(parents=True)
    source.write_text("失败输入" * 20, encoding="utf-8")

    class _FailingPipeline:
        async def ingest_one(self, raw_file):
            raise IngestError(IngestStage.PLAN, "校验失败", source=source.name)

    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    consumer = SyncConsumer(
        _FailingPipeline(),
        state,
        wiki_dir=tmp_path / "wiki",
        source_records_dir=tmp_path / "provenance",
        snapshots=service.snapshots,
    )
    worker = JobWorker(service)
    worker.register("compile", consumer.handle_job)
    jobs = service.submit_sync(src)
    assert len(jobs) == 1
    asyncio.run(worker.run_once())

    assert service.store.get(jobs[0].id).status == "failed"
    assert state.get(str(source.resolve())).hash == "", "失败绝不误标已处理"
    failures = service.issues.list(kinds={IssueKind.INGESTION_FAILURE})
    assert len(failures) == 1
    assert failures[0].retry["policy"] == "manual", "失败就是等人的账"
    assert "next_retry_at" not in failures[0].retry, "无任何自动排程字段"


def test_success_resolves_issue_and_marks_hash(tmp_path: Path):
    """重试成功：job succeeded 与 issue RESOLVED、SyncState 落账同批生效。"""
    state = SyncState(tmp_path / "watch" / "state.json")
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki", sync_state=state)
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
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
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

    IssueStore(tmp_path)  # 重新初始化触发 v3 迁移
    assert service.issues.get(issue.id).status == IssueStatus.OPEN
    with service.issues.database.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='issue_actions'"
        ).fetchone()
    assert row is None


def test_recover_stale_on_service_reinit(tmp_path: Path):
    """崩溃自愈：卡死的 running 行在下一个进程构造期回队（无常驻调度器）。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    job = service.submit(kind="compile", resource="/stale", mode="sync")
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    stale_time = (datetime.now(UTC) - timedelta(seconds=900)).isoformat()
    with service.store.database.transaction(immediate=True) as conn:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (stale_time, job.id))

    revived = JobService(tmp_path, wiki_dir=tmp_path / "wiki")  # 构造期 recover_stale
    assert revived.recovered_jobs == 1
    assert revived.store.get(job.id).status == "queued"


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
