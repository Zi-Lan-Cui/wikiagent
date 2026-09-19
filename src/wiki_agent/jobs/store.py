"""Durable execution jobs shared by watch, CLI and Web entry points.

Job 是唯一执行事实来源。关键不变式：
- I1 同一 resource 至多一个 queued/running Job——由部分唯一索引
  uq_jobs_active_resource 在数据库层强制（resource 一律规范化为
  绝对路径字符串，compile/delete/issue_retry 同族共享此身份）。
- 所有写方法支持 ``_conn`` 透传：与 issue 账本同事务提交时由调用方
  持有连接，这里禁止自开事务。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from wiki_agent.jobs.errors import DuplicateActiveJob
from wiki_agent.jobs.models import Job
from wiki_agent.persistence import Database

_ACTIVE_STATUSES = ("queued", "running")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    """SQLite-backed job state; the database is the source of truth."""

    def __init__(self, workspace: str | Path):
        self.database = Database(workspace)
        self._initialize()

    # 连接与事务

    @contextmanager
    def _tx(self, _conn: sqlite3.Connection | None = None) -> Generator[sqlite3.Connection]:
        """持 _conn 时用调用方事务（不再开新事务），否则自管 immediate 事务。"""
        if _conn is not None:
            yield _conn
            return
        with self.database.transaction(immediate=True) as db:
            yield db

    def _initialize(self) -> None:
        with self.database.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
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
                    idempotency_key TEXT UNIQUE,
                    issue_id TEXT,
                    next_run_at TEXT
                );
                """
            )
            # schema v2：老库补列（幂等，OperationalError=duplicate column）
            for ddl in (
                "ALTER TABLE jobs ADD COLUMN issue_id TEXT",
                "ALTER TABLE jobs ADD COLUMN next_run_at TEXT",
            ):
                try:
                    db.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            # 唯一索引前置迁移：既有重复在途行按 resource 保留最旧，其余转终态让位
            db.execute(
                """
                UPDATE jobs SET status = 'cancelled', stage = 'cancelled', updated_at = ?
                WHERE status IN ('queued', 'running')
                  AND rowid NOT IN (
                      SELECT MIN(rowid) FROM jobs
                      WHERE status IN ('queued', 'running') GROUP BY resource
                  )
                """,
                (_now(),),
            )
            db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_status_updated
                    ON jobs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_issue_id
                    ON jobs(issue_id) WHERE issue_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_jobs_resource_status
                    ON jobs(resource, status);
                -- I1 不变式：同一 resource 至多一个在途 job（数据库强制）
                CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_resource
                    ON jobs(resource) WHERE status IN ('queued', 'running');
                """
            )

    # 写入

    def enqueue(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        issue_id: str = "",
        next_run_at: str = "",
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        """入队一个 Job。

        幂等键命中在途行 → 返回既有行（合并语义）；撞 I1 唯一索引 →
        DuplicateActiveJob（调用方按语义吞掉或取代）。
        """
        now = _now()
        with self._tx(_conn) as db:
            if idempotency_key:
                existing = db.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ? AND status IN ('queued','running')",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._row(existing)
                # 已完成的历史行不得阻塞同一资源的新版本：保留行、释放活跃键
                db.execute(
                    "UPDATE jobs SET idempotency_key = NULL WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
            job_id = f"job_{uuid4().hex}"
            try:
                db.execute(
                    """INSERT INTO jobs
                    (id, kind, resource, mode, status, payload_json, created_at, updated_at,
                     idempotency_key, issue_id, next_run_at)
                    VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        kind,
                        resource,
                        mode,
                        json.dumps(payload or {}, ensure_ascii=False),
                        now,
                        now,
                        idempotency_key,
                        issue_id or None,
                        next_run_at or None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateActiveJob(resource) from exc
            return self.get(job_id, _conn=db)

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        error: str | None = None,
        next_run_at: str | None = None,
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        fields, values = [], []
        for name, value in (
            ("status", status),
            ("stage", stage),
            ("error", error),
            ("next_run_at", next_run_at),
        ):
            if value is None:
                continue
            if name == "next_run_at":
                value = value or None  # "" 表示立即可领 → 存 NULL
            fields.append(f"{name} = ?")
            values.append(value)
        fields.append("updated_at = ?")
        values.extend([_now(), job_id])
        with self._tx(_conn) as db:
            db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
            return self.get(job_id, _conn=db)

    def attach_issue(
        self, job_id: str, issue_id: str, *, _conn: sqlite3.Connection | None = None
    ) -> Job:
        with self._tx(_conn) as db:
            db.execute(
                "UPDATE jobs SET issue_id = ?, updated_at = ? WHERE id = ?",
                (issue_id, _now(), job_id),
            )
            return self.get(job_id, _conn=db)

    def coalesce_payload(
        self, job_id: str, payload: dict[str, object], *, _conn: sqlite3.Connection | None = None
    ) -> Job | None:
        """合并意图进尚未执行的排队行（digest 前进覆盖）；已开始执行返回 None。"""
        with self._tx(_conn) as db:
            changed = db.execute(
                "UPDATE jobs SET payload_json = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                (json.dumps(payload, ensure_ascii=False), _now(), job_id),
            ).rowcount
            if not changed:
                return None
            return self.get(job_id, _conn=db)

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        """按注册类型领取到期的排队 Job（next_run_at 未到期则跳过）。

        kinds 过滤是多进程共库下的分工边界：各进程只领自己注册了 handler
        的类型，未注册即误杀在途工作的问题不复存在。
        """
        now = _now()
        where = "status = 'queued' AND (next_run_at IS NULL OR next_run_at <= ?)"
        values: list[object] = [now]
        if kinds:
            marks = ",".join("?" for _ in kinds)
            where += f" AND kind IN ({marks})"
            values.extend(sorted(kinds))
        with self._tx() as db:
            row = db.execute(
                f"SELECT id FROM jobs WHERE {where} ORDER BY created_at LIMIT 1",
                values,
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            return self.get(str(row["id"]), _conn=db)

    def recover_stale(self, *, max_age_seconds: int = 300) -> int:
        """把超时未心跳的 running 回队（重启与卡死的对账兜底）。"""
        cutoff = datetime.now(UTC).timestamp() - max_age_seconds
        with self._tx() as db:
            rows = db.execute("SELECT id, updated_at FROM jobs WHERE status='running'").fetchall()
            stale = [
                row["id"]
                for row in rows
                if datetime.fromisoformat(row["updated_at"]).timestamp() < cutoff
            ]
            for job_id in stale:
                db.execute(
                    "UPDATE jobs SET status='queued', next_run_at=NULL, updated_at=? WHERE id=?",
                    (_now(), job_id),
                )
        return len(stale)

    # 读取

    def get(self, job_id: str, *, _conn: sqlite3.Connection | None = None) -> Job:
        if _conn is not None:
            return self._row(self._require_row(_conn, job_id))
        with self.database.connect() as connection:
            return self._row(self._require_row(connection, job_id))

    @staticmethod
    def _require_row(db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(job_id)
        return row

    def active_by_resource(
        self, resource: str, *, _conn: sqlite3.Connection | None = None
    ) -> Job | None:
        """该资源当前的在途 Job（queued/running 至多一个，I1）。"""
        if _conn is not None:
            row = _conn.execute(
                "SELECT * FROM jobs WHERE resource = ? AND status IN ('queued','running')",
                (resource,),
            ).fetchone()
            return self._row(row) if row is not None else None
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE resource = ? AND status IN ('queued','running')",
                (resource,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def open_issue_ids_with_active_job(self) -> dict[str, str]:
        """issue_id → active job_id，对账补挂关系用。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT issue_id, id FROM jobs WHERE issue_id IS NOT NULL AND status IN ('queued','running')"
            ).fetchall()
        return {str(row["issue_id"]): str(row["id"]) for row in rows}

    def has_active_job_by_issue(self, issue_id: str) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM jobs WHERE issue_id = ? AND status IN ('queued','running') LIMIT 1",
                (issue_id,),
            ).fetchone()
        return row is not None

    def list(self, *, limit: int = 100) -> list[Job]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            resource=row["resource"],
            mode=row["mode"],
            status=row["status"],
            stage=row["stage"],
            attempts=row["attempts"],
            error=row["error"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            issue_id=str(row["issue_id"] or ""),
            next_run_at=str(row["next_run_at"] or ""),
        )
