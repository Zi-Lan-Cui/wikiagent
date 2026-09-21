"""Durable execution jobs shared by watch, CLI and Web entry points.

Job 是唯一执行事实来源。关键不变式：
- 同一 resource 至多一个在途（in-flight = queued/running）Job——由部分
  唯一索引 uq_jobs_in_flight_resource 在数据库层强制（resource 一律
  规范化为绝对路径字符串，compile/delete/issue_retry 同族共享此身份）。
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

from wiki_agent.jobs.errors import DuplicateInFlightJob
from wiki_agent.jobs.models import Job
from wiki_agent.persistence import Database

# "在途"的唯一定义：queued + running。所有查询与唯一索引共用这一片段。
_IN_FLIGHT_SQL = "status IN ('queued', 'running')"


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
                    issue_id TEXT
                );
                """
            )
            # schema v2：老库补列（幂等，OperationalError=duplicate column）
            try:
                db.execute("ALTER TABLE jobs ADD COLUMN issue_id TEXT")
            except sqlite3.OperationalError:
                pass
            # schema v3：手动重试模型——排程列作废，老库的 next_run_at 删除
            try:
                db.execute("ALTER TABLE jobs DROP COLUMN next_run_at")
            except sqlite3.OperationalError:
                pass
            # 唯一索引前置迁移：既有重复在途行按 resource 保留最旧，其余转终态让位
            db.execute(
                f"""
                UPDATE jobs SET status = 'cancelled', stage = 'cancelled', updated_at = ?
                WHERE {_IN_FLIGHT_SQL}
                  AND rowid NOT IN (
                      SELECT MIN(rowid) FROM jobs
                      WHERE {_IN_FLIGHT_SQL} GROUP BY resource
                  )
                """,
                (_now(),),
            )
            db.executescript(
                f"""
                CREATE INDEX IF NOT EXISTS idx_jobs_status_updated
                    ON jobs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_issue_id
                    ON jobs(issue_id) WHERE issue_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_jobs_resource_status
                    ON jobs(resource, status);
                -- 唯一在途约束：同一 resource 至多一个在途 job（数据库强制）
                CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_in_flight_resource
                    ON jobs(resource) WHERE {_IN_FLIGHT_SQL};
                -- 索引改名的幂等迁移（旧名 active 不表达"在途"）
                DROP INDEX IF EXISTS uq_jobs_active_resource;
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
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        """入队一个 Job。

        幂等键命中在途行 → 返回既有行（合并语义）；撞唯一索引 →
        DuplicateInFlightJob（调用方按语义吞掉、收敛或取代）。
        """
        now = _now()
        with self._tx(_conn) as db:
            if idempotency_key:
                existing = db.execute(
                    f"SELECT * FROM jobs WHERE idempotency_key = ? AND {_IN_FLIGHT_SQL}",
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
                     idempotency_key, issue_id)
                    VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
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
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateInFlightJob(resource) from exc
            return self.get(job_id, _conn=db)

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        error: str | None = None,
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        fields, values = [], []
        for name, value in (("status", status), ("stage", stage), ("error", error)):
            if value is None:
                continue
            fields.append(f"{name} = ?")
            values.append(value)
        fields.append("updated_at = ?")
        values.extend([_now(), job_id])
        with self._tx(_conn) as db:
            db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
            return self.get(job_id, _conn=db)

    def try_finalize(
        self,
        job_id: str,
        *,
        status: str,
        stage: str | None = None,
        error: str | None = None,
        _conn: sqlite3.Connection | None = None,
    ) -> bool:
        """终态 CAS：仅当行仍是 running 时写入，返回是否命中。

        先到者翻转终态后，迟到写（被取代的 handler、崩溃重放的旧持有者）
        必不命中——终态与 outcome 联动只发生一次。
        """
        sets = ["status = ?"]
        values: list[object] = [status]
        if stage is not None:
            sets.append("stage = ?")
            values.append(stage)
        if error is not None:
            sets.append("error = ?")
            values.append(error)
        sets.append("updated_at = ?")
        values.extend([_now(), job_id])
        with self._tx(_conn) as db:
            return (
                db.execute(
                    f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? AND status = 'running'",
                    values,
                ).rowcount
                > 0
            )

    def attach_issue(
        self, job_id: str, issue_id: str, *, _conn: sqlite3.Connection | None = None
    ) -> Job:
        with self._tx(_conn) as db:
            db.execute(
                "UPDATE jobs SET issue_id = ?, updated_at = ? WHERE id = ?",
                (issue_id, _now(), job_id),
            )
            return self.get(job_id, _conn=db)

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        """按注册类型领取最早排队的 Job。

        kinds 过滤是多进程共库下的分工边界：各进程只领自己注册了 handler
        的类型。空集合 = 没有任何注册类型，不领任何活（不是"不过滤"）；
        None = 显式不过滤（测试/单进程）。
        """
        if kinds is not None and not kinds:
            return None
        where = "status = 'queued'"
        values: list[object] = []
        if kinds is not None:
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
                    "UPDATE jobs SET status='queued', updated_at=? WHERE id=?",
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

    def in_flight_by_resource(
        self, resource: str, *, _conn: sqlite3.Connection | None = None
    ) -> Job | None:
        """该资源当前的在途 Job（queued/running 至多一个，唯一在途约束）。"""
        query = f"SELECT * FROM jobs WHERE resource = ? AND {_IN_FLIGHT_SQL}"
        if _conn is not None:
            row = _conn.execute(query, (resource,)).fetchone()
        else:
            with self.database.connect() as db:
                row = db.execute(query, (resource,)).fetchone()
        # 唯一在途约束保证命中至多一行
        return self._row(row) if row is not None else None

    def open_issue_ids_with_in_flight_job(self) -> dict[str, str]:
        """issue_id → 在途 job_id，web"哪些问题上挂着执行"用。"""
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT issue_id, id FROM jobs WHERE issue_id IS NOT NULL AND {_IN_FLIGHT_SQL}"
            ).fetchall()
        return {str(row["issue_id"]): str(row["id"]) for row in rows}

    def list_in_flight_without_issue(self, *, kind: str = "compile") -> list[Job]:
        """在途但没挂账 issue 的 job——对账补挂关系用。"""
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT * FROM jobs WHERE kind = ? AND {_IN_FLIGHT_SQL}"
                " AND (issue_id IS NULL OR issue_id = '')",
                (kind,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def cancel_queued_running(self, kind: str, *, reason: str) -> int:
        """一次性迁移：把指定 kind 的在途行转终态让位新模型。"""
        now = _now()
        with self._tx() as db:
            changed = db.execute(
                f"UPDATE jobs SET status='cancelled', stage='cancelled', error=?, updated_at=?"
                f" WHERE kind=? AND {_IN_FLIGHT_SQL}",
                (reason[:500], now, kind),
            ).rowcount
        return int(changed)

    def count_in_flight(self) -> int:
        """在途（queued/running）行数——脚本类调用方驱动队列到空的判据。"""
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT COUNT(*) AS total FROM jobs WHERE {_IN_FLIGHT_SQL}"
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def in_flight_for_kinds(
        self, kinds: tuple[str, ...], *, _conn: sqlite3.Connection | None = None
    ) -> int:
        """指定 kind 的在途行数——sync 互斥闸（compile+delete 未空闲则不许新快照）。"""
        marks = ",".join("?" for _ in kinds)
        with self._tx(_conn) as db:
            row = db.execute(
                f"SELECT COUNT(*) AS total FROM jobs WHERE {_IN_FLIGHT_SQL} AND kind IN ({marks})",
                tuple(kinds),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def has_in_flight_job_by_issue(self, issue_id: str) -> bool:
        """该 issue 是否有在途挂账 job——"在处理"的唯一真相（jobs join，非镜像状态）。"""
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT 1 FROM jobs WHERE issue_id = ? AND {_IN_FLIGHT_SQL} LIMIT 1",
                (issue_id,),
            ).fetchone()
        return row is not None

    def in_flight_job_by_issue(
        self, issue_id: str, *, _conn: sqlite3.Connection | None = None
    ) -> Job | None:
        """该 issue 的在途挂账 job——retry 提交点的收敛预查。"""
        with self._tx(_conn) as db:
            row = db.execute(
                f"SELECT * FROM jobs WHERE issue_id = ? AND {_IN_FLIGHT_SQL}"
                " ORDER BY created_at LIMIT 1",
                (issue_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

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
        )
