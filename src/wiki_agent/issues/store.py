"""Transactional persistence for user-visible issues."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from wiki_agent.issues.models import (
    ALLOWED_STATUS_TRANSITIONS,
    InvalidIssueTransitionError,
    IssueAlreadyClaimedError,
    IssueDraft,
    IssueKind,
    IssueNotFoundError,
    IssueRecord,
    IssueSeverity,
    IssueStatus,
    JsonObject,
)
from wiki_agent.persistence import Database

_SCHEMA_VERSION = "3"


def utc_now() -> str:
    """Return a sortable timezone-aware timestamp."""
    return datetime.now(UTC).isoformat()


def issue_fingerprint(draft: IssueDraft) -> str:
    """Build a stable deduplication key from producer-independent fields."""
    if draft.fingerprint.strip():
        return draft.fingerprint.strip()
    evidence_key = "|".join(
        str(item.get("key") or item.get("path") or item.get("claim") or "")
        for item in draft.evidence
    )
    identity = {
        "kind": draft.kind.value,
        "resource": draft.resource,
        "stage": draft.origin.get("stage", ""),
        "error_code": draft.diagnostics.get("error_code", ""),
        "evidence_key": evidence_key,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class IssueStore:
    """Own the issue database and all lifecycle transactions."""

    def __init__(self, workspace: str | Path):
        self.database = Database(workspace)
        self.workspace = self.database.workspace
        self.path = self.database.path
        self._initialize()

    def _connect(self):
        return self.database.connect()

    def _transaction(self, *, immediate: bool = False):
        return self.database.transaction(immediate=immediate)

    @contextmanager
    def _tx(self, _conn: sqlite3.Connection | None = None) -> Generator[sqlite3.Connection]:
        """持 _conn 时并入调用方事务（job/issue 单事务联动），否则自管。"""
        if _conn is not None:
            yield _conn
            return
        with self._transaction(immediate=True) as db:
            yield db

    @staticmethod
    def _resource_path(draft: IssueDraft) -> str:
        """规范化来源路径列——与 Job.resource 同一身份空间（绝对路径字符串）。

        优先 context.source_path（producer 已 resolve）；退回 resource.path
        （文件名类资源，如 wiki 页）——查询侧按等值匹配，两侧写法必须同源。
        """
        return str(draft.context.get("source_path") or draft.resource.get("path") or "").strip()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS issue_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    origin_json TEXT NOT NULL,
                    resource_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    retry_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    resolution_json TEXT NOT NULL,
                    context_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_issues_status_updated
                ON issues(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_issues_kind_status
                ON issues(kind, status);

                CREATE TABLE IF NOT EXISTS issue_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                -- 子表按 issue_id 查询（事件时间线）——SQLite 不会自动为
                -- 外键列建索引，缺则全表扫；CREATE IF NOT EXISTS 对既有库同样补齐。
                CREATE INDEX IF NOT EXISTS idx_issue_events_issue
                ON issue_events(issue_id, sequence);
                """
            )
            # schema v2：resource_path 冗余列——job resource 与 issue 来源的
            # 等值匹配键（watch 提交让位于 open 失败、对账反查都靠它）。
            # ALTER 幂等（OperationalError=duplicate column）；老行按
            # context.source_path → resource.path 顺序回填。
            try:
                connection.execute("ALTER TABLE issues ADD COLUMN resource_path TEXT")
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """
                UPDATE issues SET resource_path = COALESCE(
                    NULLIF(json_extract(context_json, '$.source_path'), ''),
                    NULLIF(json_extract(resource_json, '$.path'), '')
                ) WHERE resource_path IS NULL
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_issues_resource_path ON issues(resource_path, status)"
            )
            # schema v3：执行事实只有 jobs 表——"在途"由 job 挂账 join 派生。
            # issue_actions 中间账本删除；processing 镜像态作废，存量行回落
            # open（无条件 UPDATE 安全：新代码永不写 processing，跑一次即收敛）。
            connection.execute(
                "UPDATE issues SET status = 'open', updated_at = ? WHERE status = 'processing'",
                (utc_now(),),
            )
            connection.execute("DROP TABLE IF EXISTS issue_actions")
            connection.execute(
                "INSERT OR REPLACE INTO issue_meta(key, value) VALUES('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )

    def report(self, draft: IssueDraft, *, _conn: sqlite3.Connection | None = None) -> IssueRecord:
        """Insert or merge a report using its stable fingerprint."""
        fingerprint = issue_fingerprint(draft)
        resource_path = self._resource_path(draft)
        now = utc_now()
        with self._tx(_conn) as connection:
            existing = connection.execute(
                "SELECT * FROM issues WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                current = self._row_to_record(existing)
                new_status = (
                    IssueStatus.OPEN
                    if current.status in {IssueStatus.RESOLVED, IssueStatus.DISMISSED}
                    else current.status
                )
                connection.execute(
                    """
                    UPDATE issues
                    SET status = ?, severity = ?, title = ?, summary = ?, updated_at = ?,
                        occurrences = occurrences + 1, origin_json = ?, resource_json = ?,
                        diagnostics_json = ?, retry_json = ?, evidence_json = ?,
                        resolution_json = CASE WHEN status IN ('resolved', 'dismissed')
                            THEN '{}' ELSE resolution_json END,
                        context_json = ?, resource_path = COALESCE(?, resource_path)
                    WHERE id = ?
                    """,
                    (
                        new_status.value,
                        draft.severity.value,
                        draft.title.strip(),
                        draft.summary.strip(),
                        now,
                        self._dump(draft.origin),
                        self._dump(draft.resource),
                        self._dump(draft.diagnostics),
                        self._dump(draft.retry),
                        self._dump(draft.evidence),
                        self._dump(draft.context),
                        resource_path or None,
                        current.id,
                    ),
                )
                self._append_event(
                    connection,
                    current.id,
                    "reoccurred",
                    {"previous_status": current.status.value},
                    now,
                )
                return self._get_with_connection(connection, current.id)

            issue_id = f"issue_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO issues(
                    id, kind, status, severity, title, summary, fingerprint,
                    created_at, updated_at, occurrences, origin_json, resource_json,
                    diagnostics_json, retry_json, evidence_json, resolution_json, context_json,
                    resource_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    issue_id,
                    draft.kind.value,
                    draft.status.value,
                    draft.severity.value,
                    draft.title.strip(),
                    draft.summary.strip(),
                    fingerprint,
                    now,
                    now,
                    self._dump(draft.origin),
                    self._dump(draft.resource),
                    self._dump(draft.diagnostics),
                    self._dump(draft.retry),
                    self._dump(draft.evidence),
                    self._dump(draft.context),
                    resource_path or None,
                ),
            )
            self._append_event(connection, issue_id, "reported", {}, now)
            return self._get_with_connection(connection, issue_id)

    def get(self, issue_id: str, *, _conn: sqlite3.Connection | None = None) -> IssueRecord | None:
        if _conn is not None:
            row = _conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            return self._row_to_record(row) if row is not None else None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def get_by_fingerprint(self, fingerprint: str) -> IssueRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM issues WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def require(self, issue_id: str) -> IssueRecord:
        record = self.get(issue_id)
        if record is None:
            raise IssueNotFoundError(issue_id)
        return record

    def find_pending_failures(self, source_path: str) -> list[IssueRecord]:
        """同一来源的待处理 ingestion 失败（open/blocked）——watch 提交让位查询。

        "已认领"不是 issue 状态——在途与否由 jobs 表的唯一索引表达；
        source_path 与 Job.resource 同一身份空间（绝对路径字符串）。
        """
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM issues WHERE resource_path = ?
                   AND kind = ? AND status IN ('open','blocked')
                   ORDER BY updated_at DESC""",
                (source_path, IssueKind.INGESTION_FAILURE.value),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list(
        self,
        *,
        statuses: set[IssueStatus] | None = None,
        kinds: set[IssueKind] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[IssueRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        clauses: list[str] = []
        values: list[object] = []
        if statuses:
            marks = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({marks})")
            values.extend(status.value for status in sorted(statuses, key=str))
        if kinds:
            marks = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({marks})")
            values.extend(kind.value for kind in sorted(kinds, key=str))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((limit, max(0, offset)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM issues {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", values
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def count(
        self,
        *,
        statuses: set[IssueStatus] | None = None,
        kinds: set[IssueKind] | None = None,
    ) -> int:
        """Count issues with the same filters used by :meth:`list`."""
        clauses: list[str] = []
        values: list[object] = []
        if statuses:
            marks = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({marks})")
            values.extend(status.value for status in sorted(statuses, key=str))
        if kinds:
            marks = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({marks})")
            values.extend(kind.value for kind in sorted(kinds, key=str))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM issues {where}", values
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def transition(
        self,
        issue_id: str,
        status: IssueStatus,
        *,
        resolution: JsonObject | None = None,
        expected: set[IssueStatus] | None = None,
        event: str = "status_changed",
        _conn: sqlite3.Connection | None = None,
    ) -> IssueRecord:
        """Move one issue through the state machine with optional CAS semantics."""
        now = utc_now()
        with self._tx(_conn) as connection:
            current = self._get_with_connection(connection, issue_id)
            if expected is not None and current.status not in expected:
                raise IssueAlreadyClaimedError(
                    f"{issue_id} 当前状态为 {current.status.value}，无法执行该操作"
                )
            if (
                status != current.status
                and status not in ALLOWED_STATUS_TRANSITIONS[current.status]
            ):
                raise InvalidIssueTransitionError(
                    f"不允许从 {current.status.value} 转为 {status.value}"
                )
            connection.execute(
                "UPDATE issues SET status = ?, updated_at = ?, resolution_json = ? WHERE id = ?",
                (
                    status.value,
                    now,
                    self._dump(resolution if resolution is not None else current.resolution),
                    issue_id,
                ),
            )
            self._append_event(
                connection,
                issue_id,
                event,
                {"from": current.status.value, "to": status.value},
                now,
            )
            return self._get_with_connection(connection, issue_id)

    def update_payloads(
        self,
        issue_id: str,
        *,
        retry: JsonObject | None = None,
        diagnostics: JsonObject | None = None,
        resolution: JsonObject | None = None,
        event: str = "details_updated",
        _conn: sqlite3.Connection | None = None,
    ) -> IssueRecord:
        """Update structured details without bypassing the audit stream."""
        now = utc_now()
        with self._tx(_conn) as connection:
            current = self._get_with_connection(connection, issue_id)
            connection.execute(
                """
                UPDATE issues
                SET retry_json = ?, diagnostics_json = ?, resolution_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    self._dump(retry if retry is not None else current.retry),
                    self._dump(diagnostics if diagnostics is not None else current.diagnostics),
                    self._dump(resolution if resolution is not None else current.resolution),
                    now,
                    issue_id,
                ),
            )
            self._append_event(connection, issue_id, event, {}, now)
            return self._get_with_connection(connection, issue_id)

    def events(self, issue_id: str) -> list[JsonObject]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, event, payload_json, created_at FROM issue_events "
                "WHERE issue_id = ? ORDER BY sequence",
                (issue_id,),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "event": str(row["event"]),
                "payload": self._load_object(row["payload_json"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def get_meta(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM issue_meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO issue_meta(key, value) VALUES(?, ?)", (key, value)
            )

    def _get_with_connection(self, connection: sqlite3.Connection, issue_id: str) -> IssueRecord:
        row = connection.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise IssueNotFoundError(issue_id)
        return self._row_to_record(row)

    def _append_event(
        self,
        connection: sqlite3.Connection,
        issue_id: str,
        event: str,
        payload: JsonObject,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO issue_events(issue_id, event, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (issue_id, event, self._dump(payload), created_at),
        )

    @staticmethod
    def _dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load_object(value: str) -> JsonObject:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _load_evidence(value: str) -> list[JsonObject]:
        loaded = json.loads(value)
        return (
            [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []
        )

    def _row_to_record(self, row: sqlite3.Row) -> IssueRecord:
        return IssueRecord(
            id=str(row["id"]),
            kind=IssueKind(str(row["kind"])),
            status=IssueStatus(str(row["status"])),
            severity=IssueSeverity(str(row["severity"])),
            title=str(row["title"]),
            summary=str(row["summary"]),
            fingerprint=str(row["fingerprint"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            occurrences=int(row["occurrences"]),
            origin=self._load_object(row["origin_json"]),
            resource=self._load_object(row["resource_json"]),
            diagnostics=self._load_object(row["diagnostics_json"]),
            retry=self._load_object(row["retry_json"]),
            evidence=self._load_evidence(row["evidence_json"]),
            resolution=self._load_object(row["resolution_json"]),
            context=self._load_object(row["context_json"]),
        )
