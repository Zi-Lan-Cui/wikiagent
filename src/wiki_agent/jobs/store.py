"""Durable execution jobs shared by watch, CLI and Web entry points."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from wiki_agent.jobs.models import Job
from wiki_agent.persistence import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    """SQLite-backed job state; the database is the source of truth."""

    def __init__(self, workspace: str | Path):
        self.database = Database(workspace)
        self._initialize()

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
                    idempotency_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_updated
                    ON jobs(status, updated_at);
                """
            )

    def enqueue(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        now = _now()
        with self.database.transaction(immediate=True) as db:
            if idempotency_key:
                existing = db.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ? AND status IN ('queued','running')",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._row(existing)
                # A completed job must not block a later revision of the same
                # resource; retain the historical row but release its active
                # deduplication key.
                db.execute(
                    "UPDATE jobs SET idempotency_key = NULL WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
            job_id = f"job_{uuid4().hex}"
            db.execute(
                """INSERT INTO jobs
                (id, kind, resource, mode, status, payload_json, created_at, updated_at, idempotency_key)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (
                    job_id,
                    kind,
                    resource,
                    mode,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                    now,
                    idempotency_key,
                ),
            )
            return self.get(job_id, db=db)

    def get(self, job_id: str, *, db=None) -> Job:
        if db is None:
            with self.database.connect() as connection:
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(job_id)
        return self._row(row)

    def list(self, *, limit: int = 100) -> list[Job]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def claim_next(self) -> Job | None:
        with self.database.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = _now()
            db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            return self.get(row["id"], db=db)

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> Job:
        fields, values = [], []
        for name, value in (("status", status), ("stage", stage), ("error", error)):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        fields.append("updated_at = ?")
        values.extend([_now(), job_id])
        with self.database.transaction(immediate=True) as db:
            db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
            return self.get(job_id, db=db)

    def recover_stale(self, *, max_age_seconds: int = 300) -> int:
        cutoff = datetime.now(UTC).timestamp() - max_age_seconds
        with self.database.transaction(immediate=True) as db:
            rows = db.execute("SELECT id, updated_at FROM jobs WHERE status='running'").fetchall()
            stale = [
                row["id"]
                for row in rows
                if datetime.fromisoformat(row["updated_at"]).timestamp() < cutoff
            ]
            for job_id in stale:
                db.execute(
                    "UPDATE jobs SET status='queued', updated_at=? WHERE id=?", (_now(), job_id)
                )
        return len(stale)

    @staticmethod
    def _row(row) -> Job:
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
        )
