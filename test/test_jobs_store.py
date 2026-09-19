"""Job/Issue 数据层契约回归——I1 唯一索引、迁移、claim 过滤、_conn 同事务。"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wiki_agent.issues.models import IssueDraft, IssueKind
from wiki_agent.issues.store import IssueStore
from wiki_agent.jobs import DuplicateActiveJob, JobStore

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


# I1 唯一在途


def test_i1_unique_active_per_resource(tmp_path: Path):
    """同 resource 第二个在途 job 被数据库拒绝；终态后放行新版本。"""
    store = JobStore(tmp_path)
    first = store.enqueue(kind="compile", resource="/src/a.md", mode="watch")
    try:
        store.enqueue(kind="delete", resource="/src/a.md", mode="watch")
        assert False, "I1 应拒绝同资源第二个在途 job"
    except DuplicateActiveJob as exc:
        assert exc.resource == "/src/a.md"

    store.update(first.id, status="succeeded")
    second = store.enqueue(kind="compile", resource="/src/a.md", mode="watch")
    assert second.id != first.id


def test_idempotency_key_hit_returns_existing(tmp_path: Path):
    store = JobStore(tmp_path)
    a = store.enqueue(kind="compile", resource="/x", mode="watch", idempotency_key="k1")
    b = store.enqueue(kind="compile", resource="/x", mode="watch", idempotency_key="k1")
    assert a.id == b.id
    # 终态行释放键，新版本不被历史阻塞
    store.update(a.id, status="succeeded")
    c = store.enqueue(kind="compile", resource="/x", mode="watch", idempotency_key="k1")
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
            ("job_old1", "compile", "/dup", "watch", "queued", now, now),
            ("job_old2", "compile", "/dup", "watch", "running", now, now),
            ("job_old3", "delete", "/other", "watch", "queued", now, now),
        ],
    )
    db.commit()
    db.close()

    store = JobStore(tmp_path)
    assert store.get("job_old1").status == "queued"
    assert store.get("job_old2").status == "cancelled"
    # 新列已补上且默认空
    assert store.get("job_old1").issue_id == ""
    assert store.get("job_old1").next_run_at == ""
    try:
        store.enqueue(kind="delete", resource="/dup", mode="watch")
        assert False, "迁移后唯一索引应生效"
    except DuplicateActiveJob:
        pass


def test_claim_next_filters_kinds_and_due(tmp_path: Path):
    store = JobStore(tmp_path)
    future = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
    store.enqueue(kind="compile", resource="/c", mode="watch", next_run_at=future)
    deleted = store.enqueue(kind="delete", resource="/d", mode="watch")
    assert store.claim_next(kinds={"issue_action"}) is None
    claimed = store.claim_next(kinds={"compile", "delete"})
    assert claimed is not None and claimed.id == deleted.id
    assert claimed.status == "running" and claimed.attempts == 1
    # 未到期 compile 不被领取；把到期时间改到过去后可领
    assert store.claim_next(kinds={"compile"}) is None
    with store.database.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET next_run_at = ? WHERE resource = '/c'", ("2000-01-01T00:00:00+00:00",)
        )
    assert store.claim_next(kinds={"compile"}).resource == "/c"


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
    assert job_store.active_by_resource("/abs/b.md") is None


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
    # processing（已被认领）不让位——由 claim_action 验证
    store.claim_action(record.id, "retry")
    assert store.find_pending_failures("/data/note.md") == []
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
