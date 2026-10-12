"""记忆条目事件与 fold：memory.jsonl 是唯一真相源，条目当前状态 = 事件按序折叠的结果。

事件行只追加、永不修改删除；"彻底看不见"由 expire 事件实现而非删行。
本模块是纯数据模型，文件读写归 store。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

KINDS = ("preference", "identity", "project", "knowledge", "task_state")
PERSISTENCES = ("permanent", "durable", "ephemeral")
INVALIDATE_REASONS = ("superseded", "contradicted", "obsolete")

STATUS_ACTIVE = "active"
STATUS_INVALID = "invalid"
STATUS_EXPIRED = "expired"

EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "add": ("text", "kind", "persistence", "sources", "evidence", "hash"),
    "revise": ("text",),
    "confirm": ("evidence",),
    "reactivate": ("evidence",),
    "invalidate": ("by", "reason"),
    "expire": ("reason",),
}


@dataclass
class MemoryItem:
    """fold 出的条目当前状态。"""

    id: str
    text: str
    kind: str
    persistence: str
    sources: list[str] = field(default_factory=list)
    evidence: list[int] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    status: str = STATUS_ACTIVE
    invalid_at: str | None = None
    invalid_reason: str | None = None
    invalidated_by: str | None = None


@dataclass
class MemoryEvent:
    """memory.jsonl 的一行事件。序列化只带属于该事件类型的字段。"""

    event: str
    id: str
    timestamp: str = ""
    actor: str = "dreamer"
    text: str | None = None
    kind: str | None = None
    persistence: str | None = None
    sources: list[str] = field(default_factory=list)
    evidence: list[int] = field(default_factory=list)
    reason: str | None = None
    by: str | None = None
    hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": self.event,
            "id": self.id,
            "timestamp": self.timestamp,
            "actor": self.actor,
        }
        for name in EVENT_FIELDS.get(self.event, ()):
            payload[name] = getattr(self, name)
        return payload

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> MemoryEvent:
        """容错构造：未知键忽略、缺失取默认——坏行在 store 层已被拦下。"""
        field_names = {f for f in MemoryEvent.__dataclass_fields__ if f not in ("event", "id")}
        kwargs = {name: raw[name] for name in raw if name in field_names}
        return MemoryEvent(event=str(raw["event"]), id=str(raw["id"]), **kwargs)


def canonical_hash(text: str) -> str:
    """规范化文本（大小写折叠、空白归一）的 sha1 前 12 位；完全相同文本判重。"""
    collapsed = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha1(collapsed.encode("utf-8")).hexdigest()[:12]


def _merge(old: list[int], new: list[int]) -> list[int]:
    """证据并集，保序去重。"""
    seen = set(old)
    return old + [cursor for cursor in new if not (cursor in seen or seen.add(cursor))]


def fold(events: list[MemoryEvent]) -> dict[str, MemoryItem]:
    """按序折叠事件为条目状态表。

    已 expire 的条目保留在返回结果中（status=expired）而非丢弃——
    reactivate 的"窗口已过"拒绝判断需要看到该状态；读取侧的可见性
    过滤由 MemoryOps 负责。
    """
    state: dict[str, MemoryItem] = {}
    for event in events:
        item_id = event.id
        if event.event == "add":
            state[item_id] = MemoryItem(
                id=item_id,
                text=event.text or "",
                kind=event.kind or "",
                persistence=event.persistence or "",
                sources=list(event.sources),
                evidence=list(event.evidence),
                created_at=event.timestamp,
                updated_at=event.timestamp,
                status=STATUS_ACTIVE,
            )
        elif event.event == "revise":
            if item_id in state:
                state[item_id].text = event.text or ""
                state[item_id].updated_at = event.timestamp
        elif event.event == "confirm":
            if item_id in state:
                state[item_id].evidence = _merge(state[item_id].evidence, event.evidence)
                state[item_id].updated_at = event.timestamp
        elif event.event == "invalidate":
            item = state.get(item_id)
            if item is not None:
                item.status = STATUS_INVALID
                item.invalid_at = event.timestamp
                item.invalid_reason = event.reason
                item.invalidated_by = event.by
        elif event.event == "expire":
            item = state.get(item_id)
            if item is not None:
                item.status = STATUS_EXPIRED
                item.invalid_at = item.invalid_at or event.timestamp
        elif event.event == "reactivate":
            item = state.get(item_id)
            if item is not None and item.status == STATUS_INVALID:
                item.status = STATUS_ACTIVE
                item.invalid_at = None
                item.invalid_reason = None
                item.invalidated_by = None
                item.evidence = _merge(item.evidence, event.evidence)
                item.updated_at = event.timestamp
    return state
