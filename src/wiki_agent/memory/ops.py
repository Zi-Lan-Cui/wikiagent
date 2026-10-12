"""条目级记忆操作：Dreamer apply 阶段与 memory.jsonl 之间的校验层。

每次实例化 fold 一次事件流为内存状态，校验为目标 id 的 O(1) 集合查找；
不命中即抛 OpRejected——对"模型幻觉输出匹配不上"的硬答案是拒收留证，
不是模糊匹配。方法签名可原样转 JSON Schema 注册进 ToolRegistry。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from wiki_agent.memory.items import (
    INVALIDATE_REASONS,
    KINDS,
    PERSISTENCES,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_INVALID,
    MemoryEvent,
    MemoryItem,
    canonical_hash,
    fold,
)
from wiki_agent.memory.store import MemoryStore


class OpRejected(Exception):
    """单条 op 校验失败；reason 与 detail 供 Dreamer 写入 rejected.jsonl。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class MemoryOps:
    """memory.jsonl 的唯一写入口；每个变更方法追加一条事件并同步内存状态。"""

    def __init__(
        self,
        store: MemoryStore,
        *,
        batch_range: tuple[int, int] | None = None,
        clock: Callable[[], datetime] = datetime.now,
        actor: str = "dreamer",
    ):
        """
        Args:
            store: 提供事件流读写。
            batch_range: 本批 evidence 合法区间 (dream_cursor, history_cursor]；
                None 表示程序侧调用（如 sweep），跳过证据区间校验。
            clock: 时间源，测试注入用。
            actor: 事件署名（dreamer/system/human）。
        """
        self._store = store
        self._batch = batch_range
        self._clock = clock
        self._actor = actor
        self._state: dict[str, MemoryItem] = fold(store.read_events())

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _next_id(self) -> str:
        n = 0
        for item_id in self._state:
            try:
                n = max(n, int(item_id.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return f"m_{n + 1:04d}"

    def _append(self, event: MemoryEvent) -> None:
        self._store.append_event(event)

    def _require_item(self, item_id: str, *, allow_expired: bool = False) -> MemoryItem:
        item = self._state.get(item_id)
        if item is None:
            raise OpRejected("unknown_target", f"{item_id} 不存在")
        if item.status == STATUS_EXPIRED and not allow_expired:
            raise OpRejected("expired_window_closed", f"{item_id} 已过 TTL 窗口，需重新 add")
        return item

    def _check_evidence(self, evidence: list[int]) -> None:
        # batch None 为程序侧调用（sweep），无证据区间可校验
        if self._batch is None:
            return
        if not evidence:
            raise OpRejected("empty_evidence", "evidence 不能为空")
        lo, hi = self._batch
        for cursor in evidence:
            if not isinstance(cursor, int) or cursor <= lo or cursor > hi:
                raise OpRejected("evidence_out_of_range", f"{cursor} 不在 ({lo}, {hi}]")

    def add(
        self,
        text: str,
        kind: str,
        persistence: str,
        sources: list[str],
        evidence: list[int],
    ) -> str:
        """新增条目，返回分配的 id；文本与现有 active 条目判重命中时转 confirm。"""
        if not text or not text.strip():
            raise OpRejected("empty_text", "text 为空")
        if kind not in KINDS:
            raise OpRejected("bad_kind", kind)
        if persistence not in PERSISTENCES:
            raise OpRejected("bad_persistence", persistence)
        self._check_evidence(evidence)

        text_hash = canonical_hash(text)
        for item in self._state.values():
            if item.status == STATUS_ACTIVE and canonical_hash(item.text) == text_hash:
                self.confirm(item.id, evidence)
                return item.id

        item_id = self._next_id()
        timestamp = self._now()
        self._append(
            MemoryEvent(
                event="add",
                id=item_id,
                text=text,
                kind=kind,
                persistence=persistence,
                sources=list(sources),
                evidence=list(evidence),
                hash=text_hash,
                actor=self._actor,
                timestamp=timestamp,
            )
        )
        self._state[item_id] = MemoryItem(
            id=item_id,
            text=text,
            kind=kind,
            persistence=persistence,
            sources=list(sources),
            evidence=list(evidence),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return item_id

    def revise(self, item_id: str, text: str) -> None:
        """改表述；id 不变，保证下游引用可追踪。"""
        item = self._require_item(item_id)
        if not text or not text.strip():
            raise OpRejected("empty_text", "text 为空")
        timestamp = self._now()
        self._append(
            MemoryEvent(event="revise", id=item_id, text=text, actor=self._actor, timestamp=timestamp)
        )
        item.text = text
        item.updated_at = timestamp

    def confirm(self, item_id: str, evidence: list[int]) -> None:
        """同一事实再次出现：只加重证据，不改文本。"""
        item = self._require_item(item_id)
        self._check_evidence(evidence)
        timestamp = self._now()
        self._append(
            MemoryEvent(event="confirm", id=item_id, evidence=list(evidence), timestamp=timestamp)
        )
        seen = set(item.evidence)
        item.evidence += [cursor for cursor in evidence if cursor not in seen]
        item.updated_at = timestamp

    def invalidate(self, item_id: str, by: str | None, reason: str) -> None:
        """标记失效。已失效的条目幂等为 no-op；by 必须是当前 active 条目。"""
        item = self._require_item(item_id)
        if reason not in INVALIDATE_REASONS:
            raise OpRejected("bad_reason", reason)
        if item.status == STATUS_INVALID:
            return
        if by is not None:
            by_item = self._require_item(by)
            if by_item.status != STATUS_ACTIVE:
                raise OpRejected("by_not_active", f"{by} 状态 {by_item.status}")
        timestamp = self._now()
        self._append(
            MemoryEvent(
                event="invalidate",
                id=item_id,
                by=by,
                reason=reason,
                actor=self._actor,
                timestamp=timestamp,
            )
        )
        item.status = STATUS_INVALID
        item.invalid_at = timestamp
        item.invalid_reason = reason
        item.invalidated_by = by

    def reactivate(self, item_id: str, evidence: list[int]) -> None:
        """复活 TTL 窗口内的失效条目；已 expire 的拒绝（窗口过了需重新 add）。"""
        item = self._require_item(item_id, allow_expired=True)
        if item.status == STATUS_ACTIVE:
            return
        if item.status == STATUS_EXPIRED:
            raise OpRejected("expired_window_closed", f"{item_id} 需重新 add")
        self._check_evidence(evidence)
        timestamp = self._now()
        self._append(
            MemoryEvent(
                event="reactivate",
                id=item_id,
                evidence=list(evidence),
                actor=self._actor,
                timestamp=timestamp,
            )
        )
        item.status = STATUS_ACTIVE
        item.invalid_at = None
        item.invalid_reason = None
        item.invalidated_by = None
        seen = set(item.evidence)
        item.evidence += [cursor for cursor in evidence if cursor not in seen]
        item.updated_at = timestamp

    def expire(self, item_id: str, reason: str = "ttl") -> None:
        """TTL 到期，仅程序调用（sweep）；模型不可用。"""
        item = self._require_item(item_id, allow_expired=True)
        if item.status == STATUS_EXPIRED:
            return
        timestamp = self._now()
        self._append(
            MemoryEvent(event="expire", id=item_id, reason=reason, actor="system", timestamp=timestamp)
        )
        item.status = STATUS_EXPIRED

    def all_items(self) -> list[MemoryItem]:
        """active + invalid 全量（供 LLM 输入——失效条目必须可见才有复活窗口，不含 expired）；active 在前、组内按 created_at。"""
        visible = [i for i in self._state.values() if i.status != STATUS_EXPIRED]
        visible.sort(key=lambda i: (i.status != STATUS_ACTIVE, i.created_at, i.id))
        return visible

    def active_items(self) -> list[MemoryItem]:
        """仅 active（供渲染 memory.md 投影）；按 created_at 稳定排序。"""
        items = [i for i in self._state.values() if i.status == STATUS_ACTIVE]
        items.sort(key=lambda i: (i.created_at, i.id))
        return items
