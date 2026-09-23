"""Job/Issue 数据层契约回归——唯一在途索引、迁移、claim 过滤、_conn 同事务。"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from wiki_agent.issues.models import IssueDraft, IssueKind
from wiki_agent.issues.store import IssueStore
from wiki_agent.jobs import DuplicateInFlightJob, JobStore

_LEGACY_JOBS_DDL = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    resource TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT UNIQUE
)
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 唯一在途


def test_i1_unique_active_per_resource(tmp_path: Path):
    """同 resource 第二个在途 job 被数据库拒绝；终态后放行新版本。"""
    store = JobStore(tmp_path)
    first = store.enqueue(kind="compile", resource="/src/a.md", mode="sync")
    try:
        store.enqueue(kind="delete", resource="/src/a.md", mode="sync")
        assert False, "唯一在途索引应拒绝同资源第二个在途 job"
    except DuplicateInFlightJob as exc:
        assert exc.resource == "/src/a.md"

    store.update(first.id, status="succeeded")
    second = store.enqueue(kind="compile", resource="/src/a.md", mode="sync")
    assert second.id != first.id


def test_idempotency_key_hit_returns_existing(tmp_path: Path):
    store = JobStore(tmp_path)
    a = store.enqueue(kind="compile", resource="/x", mode="sync", idempotency_key="k1")
    b = store.enqueue(kind="compile", resource="/x", mode="sync", idempotency_key="k1")
    assert a.id == b.id
    # 终态行释放键，新版本不被历史阻塞
    store.update(a.id, status="succeeded")
    c = store.enqueue(kind="compile", resource="/x", mode="sync", idempotency_key="k1")
    assert c.id != a.id


def test_legacy_duplicate_active_rows_migrated(tmp_path: Path):
    """老库带同资源多个在途行 → 迁移保留最旧、其余 cancelled，索引可用。"""
    db = sqlite3.connect(tmp_path / "state.db")
    db.executescript(_LEGACY_JOBS_DDL)
    now = _now()
    db.executemany(
        "INSERT INTO jobs(id,kind,resource,mode,status,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("job_old1", "compile", "/dup", "sync", "queued", now, now),
            ("job_old2", "compile", "/dup", "sync", "running", now, now),
            ("job_old3", "delete", "/other", "sync", "queued", now, now),
        ],
    )
    db.commit()
    db.close()

    store = JobStore(tmp_path)
    assert store.get("job_old1").status == "queued"
    assert store.get("job_old2").status == "cancelled"
    # issue_id 列已补上且默认空
    assert store.get("job_old1").issue_id == ""
    try:
        store.enqueue(kind="delete", resource="/dup", mode="sync")
        assert False, "迁移后唯一索引应生效"
    except DuplicateInFlightJob:
        pass


def test_claim_next_empty_kinds_returns_none(tmp_path: Path):
    """空 kinds = 没有注册类型，不领任何活——分工边界不许退化为不过滤。"""
    store = JobStore(tmp_path)
    store.enqueue(kind="compile", resource="/e", mode="sync")
    assert store.claim_next(kinds=set()) is None
    # None 才是显式不过滤
    assert store.claim_next(kinds=None) is not None


def test_try_finalize_cas(tmp_path: Path):
    """终态 CAS：只有 running 可翻转，迟到写不命中、不改写。"""
    store = JobStore(tmp_path)
    job = store.enqueue(kind="compile", resource="/f", mode="sync")
    store.update(job.id, status="running")
    assert store.try_finalize(job.id, status="succeeded", stage="completed")
    assert not store.try_finalize(job.id, status="failed", error="late")
    got = store.get(job.id)
    assert got.status == "succeeded" and got.error == ""


def test_claim_next_filters_kinds(tmp_path: Path):
    store = JobStore(tmp_path)
    compiled = store.enqueue(kind="compile", resource="/c", mode="sync")
    deleted = store.enqueue(kind="delete", resource="/d", mode="sync")
    assert store.claim_next(kinds={"issue_action"}) is None
    # 只领注册过的类型
    assert store.claim_next(kinds={"delete"}).id == deleted.id
    claimed = store.claim_next(kinds={"compile", "delete"})
    assert claimed is not None and claimed.id == compiled.id
    assert claimed.status == "running" and claimed.attempts == 1
    assert store.claim_next(kinds={"compile"}) is None


# _conn 同事务线程化


def test_conn_participates_in_caller_transaction(tmp_path: Path):
    """持 _conn 的 store 写方法并入外层事务——异常时 job 与 issue 一起回滚。"""
    job_store = JobStore(tmp_path)
    issue_store = IssueStore(tmp_path)

    def _draft(source_path: str) -> IssueDraft:
        return IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="t",
            summary="s",
            resource={"type": "input_file", "path": "a.md", "label": "a.md"},
            context={"source_path": source_path},
            retry={"policy": "auto_retry"},
        )

    # 提交路径：同事务写两表
    with issue_store.database.transaction(immediate=True) as conn:
        issue = issue_store.report(_draft("/abs/a.md"), _conn=conn)
        job = job_store.enqueue(
            kind="compile", resource="/abs/a.md", mode="issue_retry", issue_id=issue.id, _conn=conn
        )
    assert job_store.get(job.id).issue_id == issue.id
    assert issue_store.get(issue.id) is not None

    # 回滚路径：外层异常 → 两表都无残留
    try:
        with issue_store.database.transaction(immediate=True) as conn:
            issue_store.report(_draft("/abs/b.md"), _conn=conn)
            job_store.enqueue(kind="compile", resource="/abs/b.md", mode="issue_retry", _conn=conn)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert issue_store.find_pending_failures("/abs/b.md") == []
    assert job_store.in_flight_by_resource("/abs/b.md") is None


def test_resource_path_written_and_queried(tmp_path: Path):
    """report 落 resource_path（context.source_path 优先）；find_pending_failures 命中。"""
    store = IssueStore(tmp_path)
    draft = IssueDraft(
        kind=IssueKind.INGESTION_FAILURE,
        title="t",
        summary="s",
        resource={"type": "input_file", "path": "note.md", "label": "note.md"},
        context={"source_path": "/data/note.md"},
    )
    record = store.report(draft)
    found = store.find_pending_failures("/data/note.md")
    assert [r.id for r in found] == [record.id]
    # 在途互斥由唯一索引在 job 层表达（重复提交被吸收），issue 保持 open 可被查到
    # 无 source_path 的旧式 draft 回退 resource.path
    store.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="t2",
            summary="s2",
            resource={"type": "wiki_page", "path": "pages/x.md", "label": "x"},
        )
    )
    assert [r.title for r in store.find_pending_failures("pages/x.md")] == ["t2"]


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn(Path(tempfile.mkdtemp()))
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)


def test_claim_order_ties_break_by_insertion(tmp_path):
    """同事务入队的多行 created_at 相同——claim 顺序必须等于入队顺序。"""
    from datetime import UTC, datetime

    from wiki_agent.jobs.store import JobStore

    store = JobStore(tmp_path)
    first = store.enqueue(kind="restructure", resource="r1", mode="manual")
    second = store.enqueue(kind="restructure", resource="r2", mode="manual")
    same = datetime.now(UTC).isoformat()
    with store.database.transaction(immediate=True) as conn:
        conn.execute("UPDATE jobs SET created_at = ?", (same,))
    assert store.claim_next(kinds={"restructure"}).id == first.id
    assert store.claim_next(kinds={"restructure"}).id == second.id
