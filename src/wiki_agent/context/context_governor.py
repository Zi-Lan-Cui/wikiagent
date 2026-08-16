from typing import List
from datetime import datetime
from pathlib import Path
import re

from wiki_agent.message import Message
from wiki_agent.session import Session
from wiki_agent.message import find_first_legal_idx
from wiki_agent.utils.helpers import (
    ensure_dir,
    estimate_text_tokens,
)
from wiki_agent.log import get_logger

logger = get_logger("CONTEXT_GOVERNOR")

# 内部瞬时工具——结果是副作用确认（无时效内容），不驱逐不标注
_INTERNAL_TRANSIENT_TOOLS = frozenset({"RecordCorrection"})


def _age_label(age_seconds: float) -> str:
    """新鲜度粗粒度分桶——逐分钟变化会破坏 prompt cache 前缀稳定。"""
    if age_seconds < 60:
        return "刚刚"
    if age_seconds < 600:
        return "几分钟前"
    if age_seconds < 3600:
        return "半小时内"
    return "超过一小时"


class ContextGovernor:
    _MERGEABLE_ROLES = {"user", "assistant"}
    # 导航三件套是探索原语，输出已自限——不截断，模型需要完整结果决定下一步
    _PERSIST_EXEMPT_TOOLS = frozenset({"ReadFile", "Grep", "ListDir"})

    def __init__(self, workspace: Path, agent_config=None,
                 tool_ttl: dict[str, int] | None = None):
        """治理参数从 agent_config 取（E3 收编）——tool_ttl 保留
        为测试注入口（测试传 60 秒验证驱逐行为，生产用 config 默认）。"""
        self.workspace = workspace
        self.tmp_dir = self.workspace / "tmp"
        cfg = agent_config
        # TTL 表：config 的一个时限 → 三个可重复获得工具共用
        ttl_seconds = (
            (cfg.tool_result_ttl_minutes * 60) if cfg
            else 30 * 60
        )
        self._tool_ttl = tool_ttl if tool_ttl is not None else {
            "ReadFile": ttl_seconds,
            "Grep": ttl_seconds,
            "ListDir": ttl_seconds,
        }
        self._persist_length = cfg.tool_persist_length if cfg else 8_000
        self._safe_buffer = cfg.snip_safe_buffer if cfg else 1024
        self._snip_ratio = cfg.snip_ratio if cfg else 0.5
        self._inflight_target_ratio = cfg.inflight_target_ratio if cfg else 0.85
        self._compact_min_chars = (
            cfg.inflight_compact_min_chars if cfg else 500)

    def _merge_consecutive(self,messages:list[Message]):
        """
        合并连续相同的role消息，但是不合并tool，或者有tool_call的assisstant消息，避免调用丢失
        """
        merged:list[Message]=[]
        for m in messages:
            if(merged
                    and merged[-1].role==m.role
                    and m.role in self._MERGEABLE_ROLES
                    and not m.tool_calls
                    and not merged[-1].tool_calls 
            ):
                merged[-1].content+="\n\n"+m.content
            else:
                merged.append(m)
        return merged

    def prepare_for_llm(self,session:Session,messages:list[Message],agent_config)->list[Message]:
        """请求前消息治理流水线。

        repair 出现两次（前后各一）:
        - 前置: 修复历史本身的断裂（上次崩溃留下的孤儿调用/结果）
        - 后置: snip 按窗口截断可能切断调用-结果配对、inflight
          紧凑化替换内容也可能产生新孤儿——发出请求前必须再修一次
        """
        messages=self._merge_consecutive(messages)
        messages=self._repair_broken_history(messages=messages)
        self._process_tool_results(session=session,messages=messages)
        self._expire_stale_tool_results(messages=messages)
        messages=self._snip_by_tokens(messages,agent_config=agent_config)
        self._compact_inflight_overflow(messages=messages,
                                       agent_config=agent_config)
        messages=self._repair_broken_history(messages=messages)
        return messages

    def _get_budget(self,context_window,max_tokens):
        return context_window-max_tokens-self._safe_buffer

    def _snip_by_tokens(self,messages:list[Message],agent_config):
        """
        按token压缩，失败则返回原始消息，让llm自然失败
        """
        budget=self._get_budget(agent_config.context_windows,agent_config.max_tokens)
        if budget<=0:
            return messages

        system_messages=[m for m in messages if m.role=="system"]
        system_tokens=estimate_text_tokens(
            text="\n".join([m.text_schema for m in system_messages if m.role=="system"])
        )

        conversation_messages=[m for m in messages if m.role!="system"]
        conversation_tokens=estimate_text_tokens(
            text="\n".join([m.text_schema for m in conversation_messages if m.role!="system"])
        )

        if system_tokens>budget:
            return messages

        target=(budget-system_tokens)*self._snip_ratio

        if conversation_tokens<=target:
            return messages

        # 逆序收集（最近的在末尾停下），再反转恢复时间顺序。
        # 旧代码收集完直接拼接——输出是倒序对话（问题3回答3问题2...），
        # LLM 读到逆时间线。find_first_legal_idx 在同一列表上做，
        # 两个操作都要在反转后的时间序上进行。
        saved_tokens=0
        saved_messages_reversed:list[Message]=[]
        for message in reversed(conversation_messages):
            message_token=estimate_text_tokens(message.text_schema)
            saved_tokens+=message_token
            if saved_tokens<target:
                saved_messages_reversed.append(message)
            else:
                break

        if not saved_messages_reversed:
            return messages

        saved_messages=list(reversed(saved_messages_reversed))

        # 要保证save合法
        idx=find_first_legal_idx(saved_messages,extend_to_user=True)
        return system_messages+saved_messages[idx:]

    # ── 窗口维度紧凑化（空间不够就丢可重取结果）──────────────


    def _total_tokens(self, messages: list[Message]) -> int:
        return sum(estimate_text_tokens(m.text_schema) for m in messages)

    def _compact_inflight_overflow(
        self, messages: list[Message], agent_config,
    ) -> None:
        """窗口维度驱逐——snip 后仍超预算时，丢弃可重取工具结果。

        snip 处理"历史太长"（丢最老消息）；本步处理"单条消息太大"——
        最近一条巨型工具结果让 snip 无从下手（要么全丢要么全留）时，
        把可重取工具的结果换成占位符，让模型需要时重新调用。

        与 TTL 驱逐的互补: TTL 是时间维度（旧了就扔，窗口有空间也扔），
        本步是空间维度（窗口不够了就扔，还新鲜也扔）。同一白名单
        （self._tool_ttl 的 key = 可重取工具注册表）。
        """
        budget = self._get_budget(agent_config.context_windows,
                                  agent_config.max_tokens)
        if budget <= 0:
            return
        estimate = self._total_tokens(messages)
        if estimate <= budget:
            return

        target = int(budget * self._inflight_target_ratio)

        # 候选: 可重取工具的 tool 消息，内容够长，未紧凑化过
        tool_indexes = [
            i for i, m in enumerate(messages)
            if m.role == "tool"
            and m.tool_name in self._tool_ttl
            and m.tool_name not in _INTERNAL_TRANSIENT_TOOLS
            and len(m.content) >= self._compact_min_chars
            and "已紧凑化" not in m.content
        ]
        if not tool_indexes:
            return

        # 最新一条保留（最新结果最可能有价值——简化的 keep-recent）
        if len(tool_indexes) > 1:
            tool_indexes = tool_indexes[:-1]

        for idx in tool_indexes:
            if estimate <= budget:
                break
            m = messages[idx]
            name = m.tool_name
            old_len = len(m.content)
            m.content = (
                f"[先前 {name} 工具结果已紧凑化以适应上下文——"
                f"调用已完成，如需内容请重新调用 {name}。]"
            )
            logger.info("工具结果紧凑化: %s（%d → %d 字符）",
                        name, old_len, len(m.content))
            estimate = self._total_tokens(messages)
            if estimate <= target:
                break

    @staticmethod
    def _safe_session_dir(session_key: str) -> str:
        """session key 可能来自 --resume 用户输入，净化后作目录名，防路径穿越。"""
        return re.sub(r"[^\w\-]", "_", session_key) or "default"

    def _persist_tool_result(
            self,
            session:Session,
            message:Message
    ) -> str:
        """完整结果写文件。返回相对指针路径（相对 workspace/），
        不泄漏用户文件系统布局。"""
        safe_key = self._safe_session_dir(session.key)
        persist_path = self.tmp_dir / safe_key
        if ensure_dir(persist_path):
            file = persist_path / f"tool_result_of_{message.tool_call_id}.txt"
            file.write_text(message.content)
        return f"tmp/{safe_key}/{file.name}"

    def _maybe_persist_tool_result(
            self,
            session:Session,
            message:Message
    ):
        # 豁免工具（ReadFile/Grep/ListDir）不转存——导航原语需要完整结果
        if message.tool_name in self._PERSIST_EXEMPT_TOOLS:
            return
        content_length=len(message.content)
        if content_length > self._persist_length:
            rel = self._persist_tool_result(
                session=session,
                message=message,
            )
            message.content = (
                f"工具结果较长，已转存: {rel}"
                f"（用 ReadFile 传入该相对路径可读取完整内容）"
            )
    def _process_tool_results(self,session:Session,messages:list[Message],):
        """
        处理工具结果，如果工具返回长度较长，选择将其持久化到文件中，将内容替换为对应的文件路径
        """
        for message in messages:
            if message.role!="tool":
                continue
            self._maybe_persist_tool_result(session,message)

    # ── 工具结果新鲜度 ─────────────────────────────────────

    def _tool_age_seconds(self, message: Message) -> float:
        """工具结果年龄——经 Message.created_at 查询接口（时间戳是元数据）。"""
        created = message.created_at
        if created is None:
            return 0.0  # 时间戳损坏视为新鲜——保守不驱逐
        return (datetime.now() - created).total_seconds()

    def _stale_result_reason(self, message: Message) -> str | None:
        """可重复获得的工具结果过期判据——None 表示新鲜/豁免。"""
        if message.role != "tool" or not message.content.strip():
            return None
        tool_name = message.tool_name or ""
        if tool_name in _INTERNAL_TRANSIENT_TOOLS:
            return None
        ttl = self._tool_ttl.get(tool_name)
        if ttl is None:
            return None  # 未登记的工具不驱逐
        age = self._tool_age_seconds(message)
        if age >= ttl:
            return (
                f"该工具结果已过期（{_age_label(age)}），原内容已清除——"
                f"如需最新数据请重新调用 {tool_name}。"
            )
        return None

    def _expire_stale_tool_results(self, messages: list[Message]) -> None:
        """TTL 硬驱逐——过期工具结果替换为占位提示（模型重新调用）。

        只作用于可重复获得的工具（wiki 导航三件套）：wiki 经
        compile/refine 变化后，长对话跨轮携带的旧读取结果已失真。
        占位消息本身不设过期（防死循环——驱逐一次后模型若不理，
        下一轮重估年龄已重置，过期内容不再注入）。
        """
        for message in messages:
            reason = self._stale_result_reason(message)
            if reason is None:
                continue
            # 标记已驱逐（幂等）——重复 prepare 不二次替换
            if "已过期" in message.content:
                continue
            logger.info("工具结果过期驱逐: %s（%s）", message.tool_name,
                        _age_label(self._tool_age_seconds(message)))
            message.content = reason

    def _repair_broken_history(self,messages:list[Message]):
        """修复断裂的历史——孤儿调用补占位 / 孤儿结果移除（委托模块级工具）。"""
        messages=_repair_orphan_tool_call(messages=messages)
        messages=_remove_orphan_tool_result(messages=messages)
        return messages



# ════════════════════════════════════════════════════════════
#  历史修复工具（模块级——原 _repair_broken_history 的嵌套闭包）
# ════════════════════════════════════════════════════════════

def _get_orphan_tool_call_ids(messages: list[Message]) -> set[str]:
    """提取有调用无结果的 tool id（意外终止/截断造成）。"""
    called_no_result_id = set()
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            for tool in message.tool_calls:
                called_no_result_id.add(tool.id)
        if message.role == "tool":
            if message.tool_call_id in called_no_result_id:
                called_no_result_id.discard(message.tool_call_id)
    return called_no_result_id


def _get_orphan_tool_result_idx(messages: list[Message]) -> set[int]:
    """提取无对应调用的工具结果下标（压缩/窗口截断造成）。"""
    tool_called = set()
    orphan_idx = set()
    for idx, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            for tool in message.tool_calls:
                tool_called.add(tool.id)
        if message.role == "tool":
            if message.tool_call_id not in tool_called:
                orphan_idx.add(idx)
    return orphan_idx


def _repair_orphan_tool_call(messages: list[Message]) -> list[Message]:
    """为孤儿 tool_call 补占位 tool 消息——保证 API 请求合法。"""
    orphan_ids = _get_orphan_tool_call_ids(messages)
    if not orphan_ids:
        return messages

    repaired: list[Message] = []
    for m in messages:
        repaired.append(m)
        # 缺失回复的调用后紧跟占位结果
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                if tc.id in orphan_ids:
                    repaired.append(Message(
                        role="tool",
                        content="该工具执行中被意外打断，没有返回结果",
                        tool_call_id=tc.id,
                    ))
    return repaired


def _remove_orphan_tool_result(messages: list[Message]) -> list[Message]:
    """移除无对应调用的工具结果。"""
    orphan_idx = _get_orphan_tool_result_idx(messages)
    if orphan_idx:
        return [
            m for idx, m in enumerate(messages) if idx not in orphan_idx
        ]
    return messages
