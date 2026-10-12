"""LLM 驱动的记忆加工：会话压缩史收敛为条目事件流，memory.md 只是投影。

单次结构化调用（协议见 schema.py），拒绝多轮 agent 形态；校验层在
MemoryOps，幻觉输出按单条拒收留证，系统错误整体上抛由 dream_loop 兜底。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wiki_agent.config import MemoryConfig
from wiki_agent.conversation import Message
from wiki_agent.log import get_logger
from wiki_agent.memory.items import STATUS_ACTIVE, STATUS_INVALID
from wiki_agent.memory.ops import MemoryOps, OpRejected
from wiki_agent.memory.projection import render_debug_md, render_memory_md
from wiki_agent.memory.schema import MEMORY_OPS_RESPONSE_FORMAT
from wiki_agent.memory.store import MemoryStore

if TYPE_CHECKING:
    from wiki_agent.llm.client import LLMClient

logger = get_logger("MEMORY")

_OP_NAMES = ("add", "revise", "confirm", "invalidate", "reactivate")


class Dreamer:
    """把未处理的会话历史交给 LLM，产出入队事件操作；TTL 清理由 sweep 程序侧完成。"""

    _DREAM_PROMPT = """
        You are a memory manager. Produce a batch of operations on the current
        memory entries from this history. Merge and restructure; do not blindly
        append. Memory is about the user, not about topics.

        # Current memory entries (full folded state, invalid included)
        {items}

        # This batch of history (JSON Lines; each record has cursor/session/
        # summary; evidence may only cite the range ({lo}, {hi}])
        {history}

        # Output protocol
        Output exactly one JSON object: {{"ops": [...]}}. Fields per op:
        - op: add | revise | confirm | invalidate | reactivate
        - target: id of the entry acted on (e.g. m_0007); null for add
        - text: required for add/revise; null otherwise
        - kind: required for add: preference | identity | project | knowledge | task_state
        - persistence: required for add: permanent | durable | ephemeral
        - evidence: history cursor numbers supporting this op, inside the declared range
        - reason: invalidate only: superseded | contradicted | obsolete
        - by: invalidate only, the superseding entry id; if it is an add in this
          batch, reference it as "#index" (#0 is the first op of this batch)

        # Rules
        1. One op states exactly one fact. Facts that differ in kind or in
           persistence must be split into separate ops — never merge identity,
           preferences and project facts into a single entry.
        2. Only facts about the user and the user's own projects are memory.
           General technical knowledge the user merely asked about (definitions,
           semantics of a syscall, how-to explanations) is NOT memory — the
           answer to such a question is assistant output, not user information.
        3. Entries marked invalid are candidates, not noise: if this history
           shows one still holds, reactivate it — never add a duplicate.
           Reactivating A does not invalidate B; if both cannot hold, explicitly
           invalidate B in the same batch.
        4. persistence tiers: ephemeral = task state or temporary requests;
           anything useless within two weeks must not be recorded at all.
           durable = the user's project facts and technical decisions.
           permanent = core preferences, personality traits, stable identity.
           Never mark a temporary request as permanent.
        5. Repeated facts: use confirm to add evidence; use revise to fix wording.
        6. Every op needs in-range evidence. The system rejects ops citing
           nothing, citing out-of-range cursors, or referencing unknown ids.
        7. If nothing changes, output {{"ops": []}} — never invent ops to fill.

        # Worked examples (few-shot). Match this shape exactly.
        History record:
        {{"cursor": 41, "session": "s_x", "summary": "用户是崔子蓝，负责 wikiagent（管线为内存队列 + SQLite）。以后回答别用 emoji。刚才问的 HTTP keep-alive 与长连接区别只是查资料。今天临时赶周报，只回要点。"}}

        Correct output — facts split by kind/persistence; the HTTP question is
        general knowledge so it gets no op; the temporary weekly-report request
        is ephemeral, not a preference; "no emoji" is a stable communication
        preference so permanent; "负责 wikiagent" is a project fact (durable),
        not identity. The three persistence tiers each get one example:
        {{"ops": [
          {{"op": "add", "target": null, "kind": "identity", "persistence": "permanent",
            "text": "用户名为崔子蓝", "evidence": [41], "reason": null, "by": null}},
          {{"op": "add", "target": null, "kind": "project", "persistence": "durable",
            "text": "用户负责 wikiagent，其编译管线为内存队列 + SQLite", "evidence": [41],
            "reason": null, "by": null}},
          {{"op": "add", "target": null, "kind": "preference", "persistence": "permanent",
            "text": "回答不使用 emoji", "evidence": [41], "reason": null, "by": null}},
          {{"op": "add", "target": null, "kind": "task_state", "persistence": "ephemeral",
            "text": "当天赶周报期间回答只给要点", "evidence": [41], "reason": null, "by": null}}
        ]}}

        Supersede example — history says the wikiagent pipeline moved to Redis
          (an earlier active entry m_0002 said in-memory queue + SQLite). Add the
          new project fact as op #0, then invalidate m_0002 with by referencing
          that just-added op by position:
        {{"ops": [
          {{"op": "add", "target": null, "kind": "project", "persistence": "durable",
            "text": "wikiagent 编译管线改用 Redis 队列", "evidence": [42], "reason": null, "by": null}},
          {{"op": "invalidate", "target": "m_0002", "kind": null, "persistence": null,
            "text": null, "evidence": [42], "reason": "superseded", "by": "#0"}}
        ]}}

        Reaffirm example — the same "no emoji" fact reappears and already exists
          as active m_0005: only add evidence, never duplicate:
        {{"ops": [
          {{"op": "confirm", "target": "m_0005", "kind": null, "persistence": null,
            "text": null, "evidence": [43], "reason": null, "by": null}}
        ]}}

        Nothing-worthy example — history is only a general-knowledge lookup
          (user asked how a GC works) with no user/project fact:
        {{"ops": []}}
        """

    def __init__(
        self,
        workspace: Path,
        memory_store: MemoryStore,
        config: MemoryConfig | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.workspace = workspace
        self.memory_store = memory_store
        self.config = config if config is not None else MemoryConfig()
        self._clock = clock

    async def sweep(self) -> int:
        """TTL 清理：invalid 条目出复活窗口后落 expire 事件。纯程序，不调 LLM。

        TTL 从 invalid_at 起算——量的是"失去效力后还留多长观察期"。
        必须在无新 history 时也周期执行，故由 dream_loop 调用而非塞进 dream()。

        Returns:
            本次过期的条目数。
        """
        ops = MemoryOps(self.memory_store, clock=self._clock)
        now = self._clock()
        expired = 0
        for item in ops.all_items():
            if item.status != STATUS_INVALID:
                continue
            ttl = self.config.ttl_days.get(item.persistence, 0)
            if ttl <= 0:
                continue
            invalid_at = _parse_timestamp(item.invalid_at)
            if invalid_at is None:
                continue
            if now - invalid_at > timedelta(days=ttl):
                ops.expire(item.id)
                expired += 1
        if expired:
            self._render_projection(ops)
        return expired

    async def dream(self, llm: LLMClient) -> None:
        """一批会话史一次结构化调用；成功推进 dream 游标。

        任一 attempt 解析失败都不推进并留证；连续 max_attempts 次失败后
        整批归档并强制推进（毒丸出口）——游标不前进的话坏批次会每轮重放、
        越滚越大，最终撑爆 token 卡死整条链。

        Raises:
            传输/IO 错误原样上抛：游标不动，下一周期重试。
        """
        store = self.memory_store
        grouped_history = store.get_unprocessed_history()
        if not grouped_history:
            self._render_projection()
            return

        cursor_lo, cursor_hi = store.get_dream_cursor(), store.get_cursor()
        evidence_session = {
            int(rec["cursor"]): key for key, records in grouped_history.items() for rec in records
        }
        history_text = "\n".join(
            json.dumps(rec, ensure_ascii=False) for records in grouped_history.values() for rec in records
        )
        items_text = _render_input_items(store, self._clock)
        raw_content = ""

        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = await llm.async_invoke(
                    messages=[
                        Message(
                            role="system",
                            content=self._DREAM_PROMPT.format(
                                items=items_text, history=history_text, lo=cursor_lo, hi=cursor_hi
                            ),
                        )
                    ],
                    response_format=MEMORY_OPS_RESPONSE_FORMAT,
                    temperature=0.2,
                )
                raw_content = response.content
                op_list = _validate_batch(raw_content)
            except (json.JSONDecodeError, ValueError) as exc:
                store.append_rejected(
                    {
                        "timestamp": self._clock().isoformat(timespec="seconds"),
                        "batch_cursor": cursor_hi,
                        "attempt": attempt,
                        "reason": "schema",
                        "detail": str(exc)[:200],
                        "raw": raw_content[:2000],
                    }
                )
                if attempt >= self.config.max_attempts:
                    self._force_advance(cursor_hi, attempts=attempt)
                    return
                continue

            # 每 attempt 新建 ops：避免失败批次污染内存状态
            ops = MemoryOps(store, batch_range=(cursor_lo, cursor_hi), clock=self._clock)
            self._apply(ops, op_list, evidence_session, batch_cursor=cursor_hi)
            self._render_projection(ops)
            store.update_dream_cursor(cursor_hi)
            return

    def _apply(
        self,
        ops: MemoryOps,
        op_list: list[dict[str, Any]],
        evidence_session: dict[int, str],
        batch_cursor: int,
    ) -> int:
        """逐条应用；单条 OpRejected 不牵连同批其余，拒收留证。"""
        applied = 0
        alias: dict[int, str] = {}
        for idx, op in enumerate(op_list):
            try:
                new_id = self._apply_one(ops, op, alias, evidence_session)
                if new_id is not None:
                    alias[idx] = new_id
                applied += 1
            except OpRejected as exc:
                self.memory_store.append_rejected(
                    {
                        "timestamp": self._clock().isoformat(timespec="seconds"),
                        "batch_cursor": batch_cursor,
                        "op": op,
                        "reason": exc.reason,
                        "detail": exc.detail,
                    }
                )
        return applied

    def _apply_one(
        self,
        ops: MemoryOps,
        op: dict[str, Any],
        alias: dict[int, str],
        evidence_session: dict[int, str],
    ) -> str | None:
        name = op["op"]
        evidence = _as_evidence(op.get("evidence"))
        target = op.get("target")

        if name == "add":
            sources = self._sources(evidence, evidence_session)
            return ops.add(
                str(op.get("text") or ""),
                str(op.get("kind") or ""),
                str(op.get("persistence") or ""),
                sources,
                evidence,
            )

        if target is None:
            raise OpRejected("missing_target", f"{name} 需要 target")
        if name == "revise":
            ops.revise(str(target), str(op.get("text") or ""))
        elif name == "confirm":
            self._require_known_evidence(evidence, evidence_session)
            ops.confirm(str(target), evidence)
        elif name == "invalidate":
            by = op.get("by")
            if isinstance(by, str) and by.startswith("#"):
                alias_idx = _parse_alias(by)
                if alias_idx not in alias:
                    raise OpRejected("unknown_by", f"{by} 未指向本批成功的 add")
                by = alias[alias_idx]
            ops.invalidate(str(target), str(by) if by is not None else None, str(op.get("reason") or ""))
        elif name == "reactivate":
            self._require_known_evidence(evidence, evidence_session)
            ops.reactivate(str(target), evidence)
        else:
            raise OpRejected("unknown_op", str(name))
        return None

    @staticmethod
    def _sources(evidence: list[int], evidence_session: dict[int, str]) -> list[str]:
        """溯源由程序推导：evidence 指向的历史记录属于哪个 session。"""
        Dreamer._require_known_evidence(evidence, evidence_session)
        return sorted({evidence_session[c] for c in evidence})

    @staticmethod
    def _require_known_evidence(evidence: list[int], evidence_session: dict[int, str]) -> None:
        unknown = [c for c in evidence if c not in evidence_session]
        if unknown:
            raise OpRejected("unknown_evidence", f"{unknown} 不在本批记录中")

    def _force_advance(self, cursor: int, attempts: int) -> None:
        """毒丸出口：整批归档并强制推进，坏数据不许无限重放。"""
        self.memory_store.append_rejected(
            {
                "timestamp": self._clock().isoformat(timespec="seconds"),
                "batch_cursor": cursor,
                "reason": "poison_batch",
                "detail": f"连续 {attempts} 次输出非法，整批强制推进",
            }
        )
        self.memory_store.update_dream_cursor(cursor)
        logger.warning("dream 毒丸批次强制推进游标至 %d", cursor)

    def _render_projection(self, ops: MemoryOps | None = None) -> None:
        """投影重建幂等：内容相同则字节不变，因此无条件渲染是安全的。"""
        ops = ops if ops is not None else MemoryOps(self.memory_store, clock=self._clock)
        self.memory_store.write_projection(render_memory_md(ops.active_items()))
        if self.config.render_debug_md:
            self.memory_store.write_debug_projection(render_debug_md(ops.all_items()))


def _render_input_items(store: MemoryStore, clock: Callable[[], datetime]) -> str:
    """全量条目（含失效）带标注供 LLM 输入——只给 active 会让 TTL 退化成延迟删除。"""
    ops = MemoryOps(store, clock=clock)
    lines: list[str] = []
    for item in ops.all_items():
        if item.status == STATUS_ACTIVE:
            state = "active"
        else:
            day = (item.invalid_at or "")[:10]
            by = f", {item.invalid_reason} by {item.invalidated_by}" if item.invalidated_by else ""
            state = f"invalid since {day}{by}"
        lines.append(f"[{item.id}] ({item.kind}/{item.persistence}, {state}) {item.text}")
    return "\n".join(lines) or "（暂无记忆条目）"


def _validate_batch(raw: str) -> list[dict[str, Any]]:
    """顶层协议校验：结构不合或 op 名未知视为整批非法。"""
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("ops"), list):
        raise ValueError("输出须为含 ops 数组的 JSON 对象")
    for op in payload["ops"]:
        if not isinstance(op, dict) or op.get("op") not in _OP_NAMES:
            raise ValueError(f"非法 op: {op!r:.120}")
    return payload["ops"]


def _as_evidence(raw: Any) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OpRejected("bad_evidence", f"evidence 须为数组: {raw!r:.80}")
    try:
        return [int(c) for c in raw]
    except (TypeError, ValueError):
        raise OpRejected("bad_evidence", f"evidence 含非整数: {raw!r:.80}") from None


def _parse_alias(by: str) -> int:
    try:
        return int(by[1:])
    except ValueError:
        raise OpRejected("unknown_by", f"别名格式非法: {by}") from None


def _parse_timestamp(raw: str | None) -> datetime | None:
    """ISO 时间串 → datetime；损坏返回 None。与调用侧时间源对齐去 tz。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
