"""维护提议账本的存储层；schema 由本类维护。

手法与 issues 账本同构：显式状态列、条件更新（CAS）、事件留痕。
"未处置提议 ≤1" 由部分唯一索引在库层强制，读侧无需扫描判断。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from wiki_agent.database import Database
from wiki_agent.maintenance.models import (
    ALLOWED_RESOLUTION_TRANSITIONS,
    Disposition,
    Payload,
    Proposal,
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    Resolution,
)


def utc_now() -> str:
    """返回可排序的带时区时间戳。"""
    return datetime.now(UTC).isoformat()


def derive_title(payload: Payload) -> str:
    """按分析结果给提议行生成可读标题。"""
    if payload.get("healthy"):
        return "结构健康（无整理提议）"
    count = len(payload.get("effective") or [])
    return f"{count} 条整理提议" if count else "无可执行提议"


class ProposalStore:
    """proposals 表的存储层。"""

    def __init__(self, database: Database):
        self.database = database
        self.workspace = database.workspace
        self.path = database.path
        self._initialize()

    def _connect(self):
        return self.database.connect()

    def _transaction(self, *, immediate: bool = False):
        return self.database.transaction(immediate=immediate)

    @contextmanager
    def _tx(self, _conn: sqlite3.Connection | None = None) -> Generator[sqlite3.Connection]:
        """传入 _conn 时并入调用方事务，与 job 终态同事务落账；否则自开。"""
        if _conn is not None:
            yield _conn
            return
        with self._transaction(immediate=True) as db:
            yield db

    def _initialize(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    disposition_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT NOT NULL DEFAULT ''
                );

                -- 未处置提议至多一条由库强制，读侧不需要扫描判断。
                CREATE UNIQUE INDEX IF NOT EXISTS uq_proposals_pending
                    ON proposals(resolution) WHERE resolution = 'pending';
                CREATE INDEX IF NOT EXISTS idx_proposals_created
                    ON proposals(created_at DESC);

                CREATE TABLE IF NOT EXISTS proposal_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proposal_events
                    ON proposal_events(proposal_id, sequence);
                """
            )
            self._migrate_legacy_previews(connection)

    def _migrate_legacy_previews(self, connection: sqlite3.Connection) -> None:
        """把旧账本（终态 job 行 result_json.preview）一次性转入提议表。

        只在提议表为空时执行一次。旧行携带的 resolved_by（superseded/
        dismissed/submitted）映射为对应处置终态；缺 resolved_by 的行按
        updated_at 取最新一条为 pending，更早的置 superseded。任务行的
        result_json 转为只读归档，读侧不解析它。全新库还没有 jobs 表，跳过。
        """
        if connection.execute("SELECT 1 FROM proposals LIMIT 1").fetchone():
            return
        has_jobs = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if not has_jobs:
            return
        rows = connection.execute(
            "SELECT id, result_json, updated_at FROM jobs"
            " WHERE kind IN ('maintenance_preview', 'restructure_preview')"
            " AND status IN ('succeeded', 'failed')"
            " ORDER BY updated_at ASC, rowid ASC"
        ).fetchall()
        legacy: list[tuple[str, dict, str, str]] = []
        for row in rows:
            result = json.loads(row["result_json"] or "{}")
            preview = result.get("preview")
            if isinstance(preview, dict):
                legacy.append(
                    (
                        row["id"],
                        preview,
                        str(row["updated_at"]),
                        str(preview.get("resolved_by") or ""),
                    )
                )
        if not legacy:
            return
        # 最后一条未处置的作 pending，更早的未处置置 superseded
        latest_open = max(
            (index for index, (_, _, _, by) in enumerate(legacy) if not by),
            default=None,
        )
        for index, (job_id, preview, stamp, by) in enumerate(legacy):
            if by:
                resolution = Resolution(by) if by in set(Resolution) else Resolution.PENDING
            else:
                resolution = Resolution.PENDING if index == latest_open else Resolution.SUPERSEDED
            disposition = (
                {}
                if resolution is Resolution.PENDING
                else {"by": "migrated", "legacy": by or "superseded"}
            )
            proposal_id = f"prop_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO proposals(id, job_id, title, payload_json, resolution,
                    disposition_json, created_at, updated_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    job_id,
                    derive_title(preview),
                    self._dump(preview),
                    resolution.value,
                    self._dump(disposition),
                    stamp,
                    stamp,
                    "" if resolution is Resolution.PENDING else stamp,
                ),
            )
            self._append_event(
                connection, proposal_id, "migrated", {"resolution": resolution.value}, stamp
            )

    # 写入

    def record(
        self, job_id: str, payload: Payload, *, _conn: sqlite3.Connection | None = None
    ) -> Proposal:
        """记入一轮分析结果：既有 pending 一并置 superseded，新行入 pending。

        job_id 唯一——同一分析 job 重复记账拒绝；传入 _conn 与 job 终态
        同事务调用时，作废与插入共享同一写锁。
        """
        now = utc_now()
        with self._tx(_conn) as connection:
            if connection.execute("SELECT 1 FROM proposals WHERE job_id = ?", (job_id,)).fetchone():
                raise ProposalAlreadyResolvedError(f"该分析结果已入账: job {job_id}")
            for row in connection.execute(
                "SELECT id FROM proposals WHERE resolution = 'pending'"
            ).fetchall():
                connection.execute(
                    "UPDATE proposals SET resolution = ?, disposition_json = ?, updated_at = ?,"
                    " resolved_at = ? WHERE id = ?",
                    (
                        Resolution.SUPERSEDED.value,
                        self._dump({"by": "superseded"}),
                        now,
                        now,
                        row["id"],
                    ),
                )
                self._append_event(connection, row["id"], "superseded", {"by": "new_analysis"}, now)
            proposal_id = f"prop_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO proposals(id, job_id, title, payload_json, resolution,
                    disposition_json, created_at, updated_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, '{}', ?, ?, '')
                """,
                (
                    proposal_id,
                    job_id,
                    derive_title(payload),
                    self._dump(payload),
                    Resolution.PENDING.value,
                    now,
                    now,
                ),
            )
            self._append_event(connection, proposal_id, "recorded", {"job_id": job_id}, now)
            return self._require_with(connection, proposal_id)

    def _transition(
        self,
        connection: sqlite3.Connection,
        current: Proposal,
        target: Resolution,
        disposition: Disposition,
        event: str,
    ) -> Proposal:
        """pending→终态的唯一转换口；状态机与 CAS 合一。"""
        if target not in ALLOWED_RESOLUTION_TRANSITIONS.get(current.resolution, frozenset()):
            raise ProposalAlreadyResolvedError(
                f"{current.id} 当前为 {current.resolution.value}，不能置为 {target.value}"
            )
        now = utc_now()
        connection.execute(
            "UPDATE proposals SET resolution = ?, disposition_json = ?, updated_at = ?,"
            " resolved_at = ? WHERE id = ? AND resolution = ?",
            (
                target.value,
                self._dump(disposition),
                now,
                now,
                current.id,
                Resolution.PENDING.value,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] == 0:
            raise ProposalAlreadyResolvedError(f"{current.id} 已被并发处置")
        self._append_event(
            connection,
            current.id,
            event,
            {"from": current.resolution.value, "to": target.value},
            now,
        )
        return self._require_with(connection, current.id)

    def dismiss(self, proposal_id: str, *, _conn: sqlite3.Connection | None = None) -> Proposal:
        """否决一条提议。"""
        with self._tx(_conn) as connection:
            current = self._require_with(connection, proposal_id)
            return self._transition(
                connection, current, Resolution.DISMISSED, {"by": "dismissed"}, "dismissed"
            )

    def mark_submitted(
        self,
        proposal_id: str,
        job_ids: list[str],
        *,
        accepted: list[dict] | None = None,
        _conn: sqlite3.Connection | None = None,
    ) -> Proposal:
        """提议采纳入队：记由此产生的任务行，随提交事务写入。

        accepted 为实际采纳的单元标识（in_pages + out），子集采纳时
        审计链据此对得上提议原文。
        """
        disposition: Disposition = {"by": "submitted", "job_ids": job_ids}
        if accepted is not None:
            disposition["accepted"] = accepted
        with self._tx(_conn) as connection:
            current = self._require_with(connection, proposal_id)
            return self._transition(
                connection,
                current,
                Resolution.SUBMITTED,
                disposition,
                "submitted",
            )

    # 读取

    def get(self, proposal_id: str) -> Proposal | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            return self._row_to_proposal(row) if row else None

    def require(self, proposal_id: str) -> Proposal:
        with self._connect() as connection:
            return self._require_with(connection, proposal_id)

    def list_pending(self) -> list[Proposal]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE resolution = 'pending' ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_proposal(row) for row in rows]

    def list_all(self, *, limit: int = 100) -> list[Proposal]:
        """全部提议行，含已处置的，供审计与历史回看。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_proposal(row) for row in rows]

    def events(self, proposal_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event, payload_json, created_at FROM proposal_events"
                " WHERE proposal_id = ? ORDER BY sequence",
                (proposal_id,),
            ).fetchall()
            return [
                {
                    "event": row["event"],
                    "payload": self._load_object(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def _require_with(self, connection: sqlite3.Connection, proposal_id: str) -> Proposal:
        row = connection.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ProposalNotFoundError(f"提议不存在: {proposal_id}")
        return self._row_to_proposal(row)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection, proposal_id: str, event: str, payload: dict, now: str
    ) -> None:
        connection.execute(
            "INSERT INTO proposal_events(proposal_id, event, payload_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (proposal_id, event, ProposalStore._dump(payload), now),
        )

    @staticmethod
    def _dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _load_object(value: str) -> dict:
        loaded = json.loads(value or "{}")
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> Proposal:
        return Proposal(
            id=row["id"],
            job_id=row["job_id"],
            title=row["title"],
            resolution=Resolution(row["resolution"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
            payload=ProposalStore._load_object(row["payload_json"]),
            disposition=ProposalStore._load_object(row["disposition_json"]),
        )
