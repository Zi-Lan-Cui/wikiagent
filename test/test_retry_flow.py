"""统一执行模型提交入口验收——issue retry 建 job、双击唯一、delete 取代、
watch 让位/合并、失败不误标账、调度器只生产。

直接运行:  .venv/bin/python test/test_retry_flow.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.application.job_results import JobResult
from wiki_agent.application.job_service import JobService
from wiki_agent.application.job_worker import JobWorker
from wiki_agent.application.reconcile import MaintenanceLoop
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.issues import IssueDraft, IssueKind, IssueStatus, IssueStore
from wiki_agent.watch.consumer import WatchConsumer
from wiki_agent.watch.state import WatchState, digest_file_text


def _failure_issue(service: JobService, source: Path, *, attempts: int = 1, next_retry: str = ""):
    return service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title=f"{source.name} 处理失败",
            summary="plan 失败",
            resource={"type": "input_file", "path": source.name, "label": source.name},
            context={"source_path": str(source)},
            retry={"policy": "auto_retry", "attempts": attempts, "next_retry_at": next_retry},
        )
    )


def test_issue_retry_creates_job(tmp_path: Path):
    """验收 1：重试路径在 jobs 表新增挂账 compile job；提交不改 issue 状态。"""
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


def test_delete_supersedes_running_compile(tmp_path: Path):
    """delete 撞在途 compile → 同事务 cancel 让位；compile 挂的 issue 归还 open。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("x" * 40, encoding="utf-8")
    issue = _failure_issue(service, source)
    compile_job = service.submit_issue_retry(issue.id)

    delete_job = service.submit_watch_change(str(source.resolve()), deleted=True)
    assert delete_job is not None and delete_job.kind == "delete"
    assert service.store.get(compile_job.id).status == "cancelled"
    assert service.issues.get(issue.id).status == IssueStatus.OPEN


def test_failed_job_does_not_mark_hash(tmp_path: Path):
    """验收 4：ingest 失败后 WatchState 无记录；issue 走 auto_retry 通道。"""
    state = WatchState(tmp_path / "watch" / "state.json")
    service = JobService(tmp_path, watch_state=state, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "src" / "note.md"
    source.parent.mkdir(parents=True)
    source.write_text("失败输入" * 20, encoding="utf-8")
    digest, _ = digest_file_text(source)

    class _FailingPipeline:
        async def ingest_one(self, raw_file):
            raise IngestError(
                IngestStage.PLAN, "校验失败", source=source.name, retry_policy="auto_retry"
            )

    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    consumer = WatchConsumer(
        _FailingPipeline(),
        state,
        wiki_dir=tmp_path / "wiki",
        source_records_dir=tmp_path / "provenance",
    )
    worker = JobWorker(service)
    worker.register("compile", consumer.handle_job)
    job = service.submit_watch_change(str(source.resolve()), digest=digest)
    asyncio.run(worker.run_once())

    assert service.store.get(job.id).status == "failed"
    assert state.get(str(source.resolve())).hash == "", "失败绝不误标已处理"
    failures = service.issues.list(kinds={IssueKind.INGESTION_FAILURE})
    assert len(failures) == 1 and failures[0].retry["policy"] == "auto_retry"
    assert failures[0].retry["next_retry_at"], "首档退避在调度器时间轴上"


def test_watch_defers_to_undue_issue_and_links_due_one(tmp_path: Path):
    """未到期 auto_retry 失败 → 提交让位 None；到期 → 直接续链带 issue_id。"""
    from datetime import UTC, datetime, timedelta

    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    issue = _failure_issue(service, source.resolve(), next_retry=future)

    assert service.submit_watch_change(str(source.resolve()), digest="d1") is None

    # 到期后：事件通道与调度器共用入口——提交并挂上该 issue
    current = service.issues.require(issue.id)
    service.issues.update_payloads(issue.id, retry={**current.retry, "next_retry_at": ""})
    job = service.submit_watch_change(str(source.resolve()), digest="d1")
    assert job is not None and job.issue_id == issue.id


def test_queued_compile_coalesces_newer_digest(tmp_path: Path):
    """排队未执行的 job 被新内容合并（意图前进），不产生第二行。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    resource = str((tmp_path / "note.md").resolve())
    first = service.submit_watch_change(resource, digest="d1")
    second = service.submit_watch_change(resource, digest="d2")
    assert second is not None and second.id == first.id
    assert second.payload["digest"] == "d2"
    # 执行中的行不合并（吞掉，后继靠扫描）
    service.store.update(first.id, status="running")
    assert service.submit_watch_change(resource, digest="d3") is None
    assert service.store.get(first.id).payload["digest"] == "d2"


def test_transient_chain_blocks_watch_resubmit(tmp_path: Path):
    """未预期异常的链式 job 是在途行——watch 扫描重提交被唯一在途索引吸收。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    resource = str(source.resolve())
    service.submit_watch_change(resource, digest="d1")
    worker = JobWorker(service)

    async def crash(current, progress):
        raise RuntimeError("infra bug")

    worker.register("compile", crash)
    asyncio.run(worker.run_once())  # failed → 链式 queued（next_run_at 在未来）
    assert service.submit_watch_change(resource, digest="d2") is None, "链在途，吞掉"
    chain = service.store.in_flight_by_resource(resource)
    assert chain is not None and chain.payload.get("attempt_no") == 2


def test_maintenance_submits_only_due_and_produces_jobs(tmp_path: Path):
    """维护循环：到期/有策略/无在途才排队；永不执行 pipeline。"""
    from datetime import UTC, datetime, timedelta

    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    due = _failure_issue(service, source)
    # 不同资源另开一单（同指纹会合并进同一 issue）——这一单未到期
    later = tmp_path / "later.md"
    later.write_text("内容" * 12, encoding="utf-8")
    past = _failure_issue(service, later)
    current = service.issues.require(past.id)
    service.issues.update_payloads(
        past.id,
        retry={
            **current.retry,
            "next_retry_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )

    loop = MaintenanceLoop(service)
    assert loop.run_once()["retry_submitted"] == 1  # 未到期的不动
    active = service.store.in_flight_by_resource(str(source.resolve()))
    assert active is not None and active.issue_id == due.id
    assert service.issues.get(due.id).status == IssueStatus.OPEN  # 在途=job 挂账形状
    assert service.store.has_in_flight_job_by_issue(due.id)
    # 再来一轮：在途挡住重复排队
    assert loop.run_once()["retry_submitted"] == 0


def test_success_resolves_issue_and_marks_hash(tmp_path: Path):
    """重试成功：job succeeded 与 issue RESOLVED、WatchState 落账同批生效。"""
    state = WatchState(tmp_path / "watch" / "state.json")
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki", watch_state=state)
    source = tmp_path / "note.md"
    source.write_text("重试输入", encoding="utf-8")
    digest, text = digest_file_text(source)
    issue = _failure_issue(service, source)

    service.submit_issue_retry(issue.id)
    job = service.claim_next(kinds={"compile"})
    assert job is not None
    final = service.complete_with_outcome(
        job, JobResult(status="succeeded", detail={"digest": digest, "text": text})
    )
    assert final.status == "succeeded"
    assert service.issues.get(issue.id).status == IssueStatus.RESOLVED
    assert state.get(job.resource).hash == digest  # 成功是唯一落账点（先库后文件）


# 对账


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


def test_maintenance_relinks_active_job_to_failure(tmp_path: Path):
    """未到期的 open 失败账不会被重投，但按资源补挂到无账在途 compile。"""
    from datetime import UTC, datetime, timedelta

    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    job = service.submit_watch_change(str(source.resolve()), digest="d1")
    assert job.issue_id == ""
    issue = _failure_issue(service, source)
    service.issues.update_payloads(
        issue.id,
        retry={
            **service.issues.require(issue.id).retry,
            "next_retry_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )

    result = MaintenanceLoop(service).run_once()
    assert result["relinked"] == 1
    assert service.store.get(job.id).issue_id == issue.id


def test_retry_attaches_to_occupant_and_converges(tmp_path: Path):
    """retry 撞他人占位：返回占位者并补挂 issue_id——不新建、不抛错。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    occupant = service.submit_watch_change(str(source.resolve()), digest="d1")
    assert occupant.issue_id == ""
    issue = _failure_issue(service, source)

    result = service.submit_issue_retry(issue.id)
    assert result.id == occupant.id
    assert service.store.get(occupant.id).issue_id == issue.id
    assert service.store.count_in_flight() == 1


def test_retry_three_entries_converge(tmp_path: Path):
    """手工提交与维护循环并发同一 issue：汇到同一行（等价性回归位）。"""
    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    source = tmp_path / "note.md"
    source.write_text("内容" * 10, encoding="utf-8")
    issue = _failure_issue(service, source)

    first = service.submit_issue_retry(issue.id)
    # 维护循环到期重投——收敛返回同一行（在途挂账预滤 + 提交点防线）
    MaintenanceLoop(service).run_once()
    assert service.store.count_in_flight() == 1
    assert service.store.in_flight_by_resource(str(source.resolve())).id == first.id


def test_maintenance_recovers_stale_running(tmp_path: Path):
    """超时无心跳的 running 回队（进程崩溃自愈的第一环）。"""
    from datetime import UTC, datetime, timedelta

    service = JobService(tmp_path, wiki_dir=tmp_path / "wiki")
    job = service.submit_watch_change("/stale", digest="d")
    service.store.update(job.id, status="running")
    stale_time = (datetime.now(UTC) - timedelta(seconds=900)).isoformat()
    with service.store.database.transaction(immediate=True) as conn:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (stale_time, job.id))

    result = MaintenanceLoop(service, max_age_seconds=300).run_once()
    assert result["recovered"] == 1
    assert service.store.get(job.id).status == "queued"


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
