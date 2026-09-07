"""Transactional issue persistence in the application state database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
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
from wiki_agent.state import StateDatabase

_SCHEMA_VERSION = "1"


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
        self.database = StateDatabase(workspace)
        self.workspace = self.database.workspace
        self.path = self.database.path
        self._initialize()

    def _connect(self):
        return self.database.connect()

    def _transaction(self, *, immediate: bool = False):
        return self.database.transaction(immediate=immediate)

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

                CREATE TABLE IF NOT EXISTS issue_actions (
                    id TEXT PRIMARY KEY,
                    issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS legacy_issue_imports (
                    source TEXT NOT NULL,
                    legacy_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY(source, legacy_id)
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO issue_meta(key, value) VALUES('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )

    def report(self, draft: IssueDraft) -> IssueRecord:
        """Insert or merge a report using its stable fingerprint."""
        fingerprint = issue_fingerprint(draft)
        now = utc_now()
        with self._transaction(immediate=True) as connection:
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
                        context_json = ?
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
                    diagnostics_json, retry_json, evidence_json, resolution_json, context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, '{}', ?)
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
                ),
            )
            self._append_event(connection, issue_id, "reported", {}, now)
            return self._get_with_connection(connection, issue_id)

    def get(self, issue_id: str) -> IssueRecord | None:
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
    ) -> IssueRecord:
        """Move one issue through the state machine with optional CAS semantics."""
        now = utc_now()
        with self._transaction(immediate=True) as connection:
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
    ) -> IssueRecord:
        """Update structured details without bypassing the audit stream."""
        now = utc_now()
        with self._transaction(immediate=True) as connection:
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

    def claim_action(self, issue_id: str, action: str, payload: JsonObject | None = None) -> str:
        """Atomically claim an issue so the same operation cannot run twice."""
        now = utc_now()
        action_id = f"action_{uuid4().hex}"
        with self._transaction(immediate=True) as connection:
            current = self._get_with_connection(connection, issue_id)
            if current.status not in {IssueStatus.OPEN, IssueStatus.BLOCKED}:
                raise IssueAlreadyClaimedError(
                    f"{issue_id} 当前状态为 {current.status.value}，不能重复执行"
                )
            connection.execute(
                "UPDATE issues SET status = ?, updated_at = ? WHERE id = ?",
                (IssueStatus.PROCESSING.value, now, issue_id),
            )
            connection.execute(
                """
                INSERT INTO issue_actions(
                    id, issue_id, action, status, payload_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, '{}', ?, ?)
                """,
                (action_id, issue_id, action, self._dump(payload or {}), now, now),
            )
            self._append_event(
                connection,
                issue_id,
                "action_started",
                {"action_id": action_id, "action": action},
                now,
            )
        return action_id

    def complete_action(
        self,
        action_id: str,
        *,
        status: IssueStatus,
        result: JsonObject | None = None,
    ) -> IssueRecord:
        """Finish a claimed action and persist both result and issue state."""
        now = utc_now()
        with self._transaction(immediate=True) as connection:
            action_row = connection.execute(
                "SELECT issue_id, action, status FROM issue_actions WHERE id = ?", (action_id,)
            ).fetchone()
            if action_row is None:
                raise IssueNotFoundError(action_id)
            if action_row["status"] != "running":
                raise IssueAlreadyClaimedError(f"操作已结束: {action_id}")
            issue_id = str(action_row["issue_id"])
            current = self._get_with_connection(connection, issue_id)
            if current.status != IssueStatus.PROCESSING:
                raise InvalidIssueTransitionError(f"{issue_id} 不在 processing 状态")
            connection.execute(
                "UPDATE issue_actions SET status = 'completed', result_json = ?, updated_at = ? WHERE id = ?",
                (self._dump(result or {}), now, action_id),
            )
            connection.execute(
                "UPDATE issues SET status = ?, updated_at = ?, resolution_json = ? WHERE id = ?",
                (status.value, now, self._dump(result or {}), issue_id),
            )
            self._append_event(
                connection,
                issue_id,
                "action_completed",
                {"action_id": action_id, "action": action_row["action"], "status": status.value},
                now,
            )
            return self._get_with_connection(connection, issue_id)

    def fail_action(self, action_id: str, error: str, *, blocked: bool = False) -> IssueRecord:
        """Return a failed claim to open/blocked while retaining its audit trail."""
        now = utc_now()
        target = IssueStatus.BLOCKED if blocked else IssueStatus.OPEN
        with self._transaction(immediate=True) as connection:
            action_row = connection.execute(
                "SELECT issue_id, action, status FROM issue_actions WHERE id = ?", (action_id,)
            ).fetchone()
            if action_row is None:
                raise IssueNotFoundError(action_id)
            if action_row["status"] != "running":
                raise IssueAlreadyClaimedError(f"操作已结束: {action_id}")
            issue_id = str(action_row["issue_id"])
            connection.execute(
                "UPDATE issue_actions SET status = 'failed', result_json = ?, updated_at = ? WHERE id = ?",
                (self._dump({"error": error[:1000]}), now, action_id),
            )
            connection.execute(
                "UPDATE issues SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, now, issue_id),
            )
            self._append_event(
                connection,
                issue_id,
                "action_failed",
                {"action_id": action_id, "action": action_row["action"], "error": error[:1000]},
                now,
            )
            return self._get_with_connection(connection, issue_id)

    def recover_interrupted_actions(self, *, reason: str = "process_restarted") -> int:
        """Move orphaned running actions to blocked after a process interruption."""
        now = utc_now()
        recovered = 0
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id, issue_id, action FROM issue_actions WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                issue_id = str(row["issue_id"])
                connection.execute(
                    """
                    UPDATE issue_actions
                    SET status = 'failed', result_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (self._dump({"error": reason}), now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE issues
                    SET status = ?, resolution_json = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        IssueStatus.BLOCKED.value,
                        self._dump({"action": row["action"], "error": reason}),
                        now,
                        issue_id,
                        IssueStatus.PROCESSING.value,
                    ),
                )
                self._append_event(
                    connection,
                    issue_id,
                    "action_interrupted",
                    {"action_id": row["id"], "action": row["action"], "reason": reason},
                    now,
                )
                recovered += 1
        return recovered

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

    def legacy_imported(self, source: str, legacy_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM legacy_issue_imports WHERE source = ? AND legacy_id = ?",
                (source, legacy_id),
            ).fetchone()
        return row is not None

    def mark_legacy_imported(self, source: str, legacy_id: str, issue_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO legacy_issue_imports(source, legacy_id, issue_id, imported_at)
                VALUES (?, ?, ?, ?)
                """,
                (source, legacy_id, issue_id, utc_now()),
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
