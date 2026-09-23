"""sync 快照制契约——磁盘−账本=批次、互斥串行、失败即脏、成功销账。

直接运行:  .venv/bin/python test/test_sync.py
"""

from pathlib import Path

from wiki_agent.issues import IssueDraft, IssueKind, IssueStatus
from wiki_agent.jobs import JobResult, SyncInProgress
from wiki_agent.jobs.service import JobService
from wiki_agent.sync.state import SyncState, digest_file_text


def _svc(tmp: Path):
    src = tmp / "materials"
    src.mkdir(parents=True, exist_ok=True)
    wiki = tmp / "wiki"
    wiki.mkdir(exist_ok=True)
    state = SyncState(tmp / "watch" / "state.json")
    return src, JobService(tmp, wiki_dir=wiki, sync_state=state)


def _write(src: Path, name: str, content: str) -> Path:
    f = src / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f


def test_submit_sync_snapshot_diff(tmp_path: Path):
    """新文件/改过的入 compile、账上消失的入 delete、账实相符的不动。"""
    src, service = _svc(tmp_path)
    clean = _write(src, "clean.md", "稳定内容" * 10)
    gone = _write(src, "gone.md", "将被删除" * 10)
    digest_clean, text_clean = digest_file_text(clean)
    digest_gone, text_gone = digest_file_text(gone)
    service.sync_state.record(str(clean.resolve()), digest_clean, text_clean)
    service.sync_state.record(str(gone.resolve()), digest_gone, text_gone)

    _write(src, "new.md", "全新文件" * 10)
    changed = _write(src, "changed.md", "旧版本内容" * 10)
    digest_old, text_old = digest_file_text(changed)
    service.sync_state.record(str(changed.resolve()), digest_old, text_old)
    _write(src, "changed.md", "新版本内容" * 10)
    gone.unlink()

    jobs = service.submit_sync(src)
    by_resource = {Path(j.resource).name: j for j in jobs}
    assert set(by_resource) == {"new.md", "changed.md", "gone.md"}
    assert by_resource["new.md"].kind == "compile" and by_resource["new.md"].mode == "sync"
    assert by_resource["changed.md"].payload["digest"] == digest_file_text(changed)[0]
    assert by_resource["gone.md"].kind == "delete" and by_resource["gone.md"].payload["deleted"]


def test_submit_sync_excludes_roster_entries(tmp_path: Path):
    """名册式空条目（hash=""）不算 removed：从未入账，无账可清。"""
    src, service = _svc(tmp_path)
    ghost = src / "ghost.md"
    service.sync_state.set(str(ghost.resolve()), service.sync_state.get(str(ghost.resolve())))
    assert service.submit_sync(src) == []


def test_submit_sync_serial_mutex(tmp_path: Path):
    """互斥串行：compile/delete 有在途即拒绝；issue_action 不挡 sync。"""
    src, service = _svc(tmp_path)
    _write(src, "a.md", "内容" * 10)
    jobs = service.submit_sync(src)
    assert len(jobs) == 1
    try:
        service.submit_sync(src)
        assert False, "上一批未结束应拒绝新快照"
    except SyncInProgress:
        pass

    # 真成功（outcome 落账）才让 a.md 变干净——直接改行不落账，快照会再拿它
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    digest, text = digest_file_text(src / "a.md")
    service.complete_with_outcome(
        claimed,
        JobResult(
            status="succeeded",
            detail={"settlement": "ingested", "digest": digest, "text": text},
        ),
    )

    service.submit(kind="issue_action", resource="iss-1", mode="rescan")
    _write(src, "b.md", "新内容" * 10)
    again = service.submit_sync(src)  # rescan 在途不挡源队列快照
    assert [Path(j.resource).name for j in again] == ["b.md"]


def test_failure_keeps_dirty_resync_is_retry(tmp_path: Path):
    """失败不写账 → 内容保持脏 → 再次 sync 就是重试（无任何排程）。"""
    src, service = _svc(tmp_path)
    f = _write(src, "fail.md", "会失败的内容" * 10)
    digest = digest_file_text(f)[0]

    jobs = service.submit_sync(src)
    assert len(jobs) == 1 and jobs[0].payload["digest"] == digest
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    service.complete_with_outcome(claimed, JobResult(status="failed", detail={"error": "boom"}))
    assert service.sync_state.get(str(f.resolve())).hash == ""

    # 失败行已终态（队列空闲）→ 再次 sync：同内容重新入队——这就是重试
    resync = service.submit_sync(src)
    assert len(resync) == 1 and resync[0].payload["digest"] == digest


def test_success_records_and_links_issue(tmp_path: Path):
    """成功：记实际读到的 digest、挂账 issue RESOLVED；再 sync 干净出空批。"""
    src, service = _svc(tmp_path)
    f = _write(src, "note.md", "内容" * 10)
    digest, text = digest_file_text(f)
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="note.md 处理失败",
            summary="s",
            resource={"type": "input_file", "path": "note.md", "label": "note.md"},
            context={"source_path": str(f.resolve())},
        )
    )

    jobs = service.submit_sync(src)
    assert jobs[0].issue_id == issue.id  # 快照提交时顺手挂账
    assert service.issues.get(issue.id).status == IssueStatus.OPEN  # 提交不改账
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    service.complete_with_outcome(
        claimed,
        JobResult(
            status="succeeded",
            detail={"settlement": "ingested", "digest": digest, "text": text},
        ),
    )
    assert service.sync_state.get(str(f.resolve())).hash == digest
    resolved = service.issues.get(issue.id)
    assert resolved.status == IssueStatus.RESOLVED
    # resolution 只留可追溯小字段——text/档案页全文不进问题账本
    assert set(resolved.resolution) == {"fixed_by", "digest", "settlement"}
    assert service.submit_sync(src) == []


def test_delete_resolves_linked_failure_record(tmp_path: Path):
    """删除任务与 compile 同一挂账规则：成功删除关闭该来源的全部活动失败记录。"""
    src, service = _svc(tmp_path)
    f = _write(src, "gone.md", "将被删除" * 10)
    digest, text = digest_file_text(f)
    service.sync_state.record(str(f.resolve()), digest, text)
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="gone.md 处理失败",
            summary="s",
            resource={"type": "input_file", "path": "gone.md", "label": "gone.md"},
            context={"source_path": str(f.resolve())},
        )
    )
    f.unlink()

    jobs = service.submit_sync(src)
    assert [j.kind for j in jobs] == ["delete"] and jobs[0].issue_id == issue.id
    claimed = service.claim_next(kinds={"delete"})
    assert claimed is not None
    service.complete_with_outcome(
        claimed, JobResult(status="succeeded", detail={"settlement": "delete_applied"})
    )
    record = service.issues.get(issue.id)
    assert record.status == IssueStatus.RESOLVED
    assert record.resolution["cause"] == "source_deleted"
    assert service.sync_state.get(str(f.resolve())).hash == "", "完成账条目随之移除"


def test_orphan_failure_closed_at_submit(tmp_path: Path):
    """点击前就消失、又从未入账的失败记录：提交时按事实关闭，不再等人工逐条清理。"""
    src, service = _svc(tmp_path)
    ghost_abs = str((src / "ghost.md").resolve())
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="ghost.md 处理失败",
            summary="s",
            resource={"type": "input_file", "path": "ghost.md", "label": "ghost.md"},
            context={"source_path": ghost_abs},
        )
    )
    assert service.submit_sync(src) == []  # 无任务可拍
    record = service.issues.get(issue.id)
    assert record.status == IssueStatus.RESOLVED
    assert record.resolution["cause"] == "source_deleted"
    assert record.resolution["closed_by"] == "submit_sync"


def test_succeeded_without_settlement_records_ledger_but_not_issue(tmp_path: Path):
    """两条通道正交：无 settlement 的成功照样落完成账，但不动问题账本。"""
    src, service = _svc(tmp_path)
    f = _write(src, "note.md", "内容" * 10)
    digest, text = digest_file_text(f)
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="note.md 处理失败",
            summary="s",
            resource={"type": "input_file", "path": "note.md", "label": "note.md"},
            context={"source_path": str(f.resolve())},
        )
    )
    claimed = service.submit_sync(src)[0]
    claimed = service.claim_next(kinds={"compile"})
    assert claimed is not None
    service.complete_with_outcome(
        claimed, JobResult(status="succeeded", detail={"digest": digest, "text": text})
    )
    assert service.sync_state.get(str(f.resolve())).hash == digest, "完成账照常"
    assert service.issues.get(issue.id).status == IssueStatus.OPEN, "未申报类别不动账本"


def test_sync_status_readonly(tmp_path: Path):
    src, service = _svc(tmp_path)
    _write(src, "x.md", "内容" * 10)
    st = service.sync_status(src)
    assert st == {"dirty": 1, "removed": 0, "in_flight": 0}
    service.submit_sync(src)
    st2 = service.sync_status(src)
    assert st2["dirty"] == 1  # 快照已入队但账未记——status 如实呈现
    assert st2["in_flight"] == 1


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        try:
            fn(Path(tempfile.mkdtemp()))
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
