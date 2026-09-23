"""SnapshotStore：定格、不可变读、相对路径、故障分类、启动清扫。

直接运行:  .venv/bin/python test/test_snapshots.py
"""


from pathlib import Path

import pytest

from wiki_agent.snapshots import SnapshotError, SnapshotStore


def _src(tmp: Path) -> Path:
    root = tmp / "materials"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("内容甲", encoding="utf-8")
    (root / "sub" / "b.md").write_text("内容乙", encoding="utf-8")
    return root


def test_capture_freezes_content_and_returns_digests(tmp_path: Path):
    root = _src(tmp_path)
    store = SnapshotStore(tmp_path)
    captured = store.capture("b1", root, [root / "a.md", root / "sub" / "b.md"])
    assert set(captured) == {str((root / "a.md").resolve()), str((root / "sub" / "b.md").resolve())}
    # 原件随后被改，快照件不动——capture 的意义就在于此
    (root / "a.md").write_text("改过的内容", encoding="utf-8")
    assert store.staged_path("b1", "a.md").read_text(encoding="utf-8") == "内容甲"
    assert store.staged_path("b1", "sub/b.md").read_text(encoding="utf-8") == "内容乙"
    # 相对路径保真：批目录内就是源目录的镜像结构
    assert (store.root / "b1" / "sub" / "b.md").is_file()
    # 不留临时件
    assert [p.name for p in (store.root / "b1").rglob("*.copying")] == []


def test_capture_rejects_missing_and_escaping_paths(tmp_path: Path):
    root = _src(tmp_path)
    store = SnapshotStore(tmp_path)
    with pytest.raises(SnapshotError):
        store.capture("b2", root, [root / "nope.md"])
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(SnapshotError, match="不在源目录内"):
        store.capture("b3", root, [outside])


def test_drop_and_sweep(tmp_path: Path):
    root = _src(tmp_path)
    store = SnapshotStore(tmp_path)
    store.capture("keep", root, [root / "a.md"])
    store.capture("gone", root, [root / "a.md"])
    store.capture("ghost", root, [root / "a.md"])
    store.drop_batch("gone")
    assert not (store.root / "gone").exists()
    removed = store.sweep_orphans(live_batches={"keep"})
    assert removed == 1 and (store.root / "keep").is_dir() and not (store.root / "ghost").exists()
    # 没有仓库目录时清扫是 no-op
    fresh = SnapshotStore(tmp_path / "empty-ws")
    assert fresh.sweep_orphans(set()) == 0


def test_orphan_sweep_at_service_construction(tmp_path: Path):
    """模拟崩溃：入队后任务被清成终态，重建 JobService 时无主快照目录被扫掉。"""
    from wiki_agent.jobs.service import JobService
    from wiki_agent.sync.state import SyncState

    root = _src(tmp_path)
    ws = tmp_path / "ws"
    service = JobService(
        ws, wiki_dir=tmp_path / "wiki", sync_state=SyncState(ws / "watch" / "state.json")
    )
    jobs = service.submit_sync(root)
    batch = str(jobs[0].payload["batch"])
    assert (service.snapshots.root / batch).is_dir()
    # 该批任务全部终态（模拟处理完后崩溃前没走到 GC——重启清扫兜底）
    for job in jobs:
        service.store.update(job.id, status="succeeded", stage="completed")
    revived = JobService(
        ws, wiki_dir=tmp_path / "wiki", sync_state=SyncState(ws / "watch" / "state.json")
    )
    assert not (revived.snapshots.root / batch).exists()


def test_last_terminal_job_drops_batch_snapshot(tmp_path: Path):
    """GC 主规则：批内最后一个任务进入终态 → 快照目录即删。"""
    from wiki_agent.jobs import JobResult
    from wiki_agent.jobs.service import JobService
    from wiki_agent.sync.state import SyncState

    root = _src(tmp_path)
    ws = tmp_path / "ws"
    service = JobService(
        ws, wiki_dir=tmp_path / "wiki", sync_state=SyncState(ws / "watch" / "state.json")
    )
    jobs = service.submit_sync(root)
    batch = str(jobs[0].payload["batch"])
    assert (service.snapshots.root / batch).is_dir()
    first = service.claim_next(kinds={"compile"})
    service.complete_with_outcome(first, JobResult(status="succeeded", detail={}))
    assert (service.snapshots.root / batch).is_dir(), "还有在途任务，快照不能删"
    second = service.claim_next(kinds={"compile"})
    service.complete_with_outcome(second, JobResult(status="succeeded", detail={}))
    assert not (service.snapshots.root / batch).exists()


def test_capture_survives_existing_dir(tmp_path: Path):
    """同批重捕获（异常场景）不炸；目录复用 shutil 语义。"""
    root = _src(tmp_path)
    store = SnapshotStore(tmp_path)
    d1 = store.capture("b4", root, [root / "a.md"])
    d2 = store.capture("b4", root, [root / "a.md"])
    assert d1 == d2


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        tmp = Path(__import__("tempfile").mkdtemp())
        try:
            fn(tmp)
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            import traceback

            traceback.print_exc()
    raise SystemExit(1 if failed else 0)
