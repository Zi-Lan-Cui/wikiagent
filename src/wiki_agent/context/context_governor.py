import re
from datetime import datetime
from pathlib import Path

from wiki_agent.conversation import Message, Session, find_first_legal_idx
from wiki_agent.log import get_logger
from wiki_agent.utils import (
    ensure_dir,
    estimate_text_tokens,
)

logger = get_logger("CONTEXT_GOVERNOR")

# 内部瞬时工具：结果只确认副作用，无时效性，不参与驱逐
_INTERNAL_TRANSIENT_TOOLS = frozenset({"RecordCorrection"})


def _age_label(age_seconds: float) -> str:
    """新鲜度粗粒度分桶。

    精确到分钟会破坏 prompt cache 前缀稳定，桶粒度保证占位文本会话内不变。

    Args:
        age_seconds: 年龄（秒）。

    Returns:
        分桶标签（刚刚/几分钟前/半小时内/超过一小时）。
    """
    if age_seconds < 60:
        return "刚刚"
    if age_seconds < 600:
        return "几分钟前"
    if age_seconds < 3600:
        return "半小时内"
    return "超过一小时"


class ContextGovernor:
    _MERGEABLE_ROLES = {"user", "assistant"}
    # ReadFile/Grep/ListDir 输出量已由工具自身限制，不转存，
    # 模型需要完整结果决定下一步
    _PERSIST_EXEMPT_TOOLS = frozenset({"ReadFile", "Grep", "ListDir"})

    def __init__(self, workspace: Path, agent_config=None, tool_ttl: dict[str, int] | None = None):
        """初始化上下文治理器。

        Args:
            workspace: 工作区（tmp/ 转存目录的根）。
            agent_config: AgentConfig，治理参数来源；None 用默认。
            tool_ttl: 工具名 → TTL 秒数覆盖表，测试注入用。
        """
        self.workspace = workspace
        self.tmp_dir = self.workspace / "tmp"
        cfg = agent_config
        ttl_seconds = (cfg.tool_result_ttl_minutes * 60) if cfg else 30 * 60
        self._tool_ttl = (
            tool_ttl
            if tool_ttl is not None
            else {
                "ReadFile": ttl_seconds,
                "Grep": ttl_seconds,
                "ListDir": ttl_seconds,
            }
        )
        self._persist_length = cfg.tool_persist_length if cfg else 8_000
        self._safe_buffer = cfg.snip_safe_buffer if cfg else 1024
        self._snip_ratio = cfg.snip_ratio if cfg else 0.5
        self._inflight_target_ratio = cfg.inflight_target_ratio if cfg else 0.85
        self._compact_min_chars = cfg.inflight_compact_min_chars if cfg else 500

    def _merge_consecutive(self, messages: list[Message]):
        """
        合并连续相同 role 的消息。

        不合并 tool 消息或带 tool_call 的 assistant 消息——
        避免调用配对丢失。

        Args:
            messages: 原始消息列表。

        Returns:
            合并后的消息列表。
        """
        merged: list[Message] = []
        for m in messages:
            if (
                merged
                and merged[-1].role == m.role
                and m.role in self._MERGEABLE_ROLES
                and not m.tool_calls
                and not merged[-1].tool_calls
            ):
                merged[-1].content += "\n\n" + m.content
            else:
                merged.append(m)
        return merged

    def prepare_for_llm(
        self, session: Session, messages: list[Message], agent_config
    ) -> list[Message]:
        """请求前消息治理流水线。

        repair 前后各执行一次：前置修复历史中已有的孤儿调用/结果；
        snip 截断和 inflight 紧凑化可能切断调用-结果配对或产生新孤儿，
        发出请求前需再修一次。

        Args:
            session: 会话（转存/驱逐的归属）。
            messages: 工作副本消息（内部会修改 tool 消息内容）。
            agent_config: AgentConfig（预算参数）。

        Returns:
            治理后的消息列表（可发给 LLM）。
        """
        messages = self._merge_consecutive(messages)
        messages = self._repair_broken_history(messages=messages)
        self._process_tool_results(session=session, messages=messages)
        self._expire_stale_tool_results(messages=messages)
        messages = self._snip_by_tokens(messages, agent_config=agent_config)
        self._compact_inflight_overflow(messages=messages, agent_config=agent_config)
        messages = self._repair_broken_history(messages=messages)
        return messages

    def _get_budget(self, context_window, max_tokens):
        return context_window - max_tokens - self._safe_buffer

    def _snip_by_tokens(self, messages: list[Message], agent_config):
        """按 token 预算截断对话部分（保留 system）。

        从最近的消息逆序收集到预算后反转回时间序。截断失败时
        返回原始消息，交由 LLM 报错。

        Args:
            messages: 消息列表。
            agent_config: AgentConfig（上下文窗口/生成上限）。

        Returns:
            截断后的消息列表（预算不够时原样返回）。
        """
        budget = self._get_budget(agent_config.context_windows, agent_config.max_tokens)
        if budget <= 0:
            return messages

        system_messages = [m for m in messages if m.role == "system"]
        system_tokens = estimate_text_tokens(
            text="\n".join([m.text_schema for m in system_messages if m.role == "system"])
        )

        conversation_messages = [m for m in messages if m.role != "system"]
        conversation_tokens = estimate_text_tokens(
            text="\n".join([m.text_schema for m in conversation_messages if m.role != "system"])
        )

        if system_tokens > budget:
            return messages

        target = (budget - system_tokens) * self._snip_ratio

        if conversation_tokens <= target:
            return messages

        # 逆序收集后反转回时间序；find_first_legal_idx 也必须在
        # 反转后的列表上操作
        saved_tokens = 0
        saved_messages_reversed: list[Message] = []
        for message in reversed(conversation_messages):
            message_token = estimate_text_tokens(message.text_schema)
            saved_tokens += message_token
            if saved_tokens < target:
                saved_messages_reversed.append(message)
            else:
                break

        if not saved_messages_reversed:
            return messages

        saved_messages = list(reversed(saved_messages_reversed))

        idx = find_first_legal_idx(saved_messages, extend_to_user=True)
        return system_messages + saved_messages[idx:]

    # inflight 紧凑化：空间不够时丢弃可重取的工具结果

    def _total_tokens(self, messages: list[Message]) -> int:
        return sum(estimate_text_tokens(m.text_schema) for m in messages)

    def _compact_inflight_overflow(
        self,
        messages: list[Message],
        agent_config,
    ) -> None:
        """snip 后仍超预算时，把可重取工具结果换成占位符。

        snip 丢最老消息，处理历史过长；本步处理单条消息过大——
        最近的巨型工具结果使 snip 只能全丢或全留时，将可重取结果
        替换为占位符，模型需要时重新调用。

        与 TTL 驱逐互补：TTL 按时间驱逐（过期即驱逐），本步按空间
        驱逐（超预算即驱逐）。两者共用 self._tool_ttl 登记的可重取工具集合。

        Args:
            messages: 消息列表（就地修改 tool 消息内容）。
            agent_config: AgentConfig（预算参数）。
        """
        budget = self._get_budget(agent_config.context_windows, agent_config.max_tokens)
        if budget <= 0:
            return
        estimate = self._total_tokens(messages)
        if estimate <= budget:
            return

        target = int(budget * self._inflight_target_ratio)

        tool_indexes = [
            i
            for i, m in enumerate(messages)
            if m.role == "tool"
            and m.tool_name in self._tool_ttl
            and m.tool_name not in _INTERNAL_TRANSIENT_TOOLS
            and len(m.content) >= self._compact_min_chars
            and "已紧凑化" not in m.content
        ]
        if not tool_indexes:
            return

        # 保留最新一条结果，其余按顺序紧凑化
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
            logger.info("工具结果紧凑化: %s（%d → %d 字符）", name, old_len, len(m.content))
            estimate = self._total_tokens(messages)
            if estimate <= target:
                break

    @staticmethod
    def _safe_session_dir(session_key: str) -> str:
        """净化 session key 为安全目录名。

        session key 可能来自 --resume 用户输入，需防路径穿越。

        Args:
            session_key: 原始 session key。

        Returns:
            净化后的目录名（非法字符替换为 _，空值用 "default"）。
        """
        return re.sub(r"[^\w\-]", "_", session_key) or "default"

    def _persist_tool_result(self, session: Session, message: Message) -> str:
        """完整结果写文件。

        Args:
            session: 会话（转存目录归属）。
            message: 要转存的 tool 消息。

        Returns:
            相对 workspace/ 的指针路径，避免暴露文件系统布局。
        """
        safe_key = self._safe_session_dir(session.key)
        persist_path = self.tmp_dir / safe_key
        if ensure_dir(persist_path):
            file = persist_path / f"tool_result_of_{message.tool_call_id}.txt"
            file.write_text(message.content)
        return f"tmp/{safe_key}/{file.name}"

    def _maybe_persist_tool_result(self, session: Session, message: Message):
        # 探索类工具（ReadFile/Grep/ListDir）不转存，模型需要完整结果
        if message.tool_name in self._PERSIST_EXEMPT_TOOLS:
            return
        content_length = len(message.content)
        if content_length > self._persist_length:
            rel = self._persist_tool_result(
                session=session,
                message=message,
            )
            message.content = (
                f"工具结果较长，已转存: {rel}（用 ReadFile 传入该相对路径可读取完整内容）"
            )

    def _process_tool_results(
        self,
        session: Session,
        messages: list[Message],
    ):
        """处理工具结果：超长结果转存文件，内容替换为相对路径。

        Args:
            session: 会话（转存目录归属）。
            messages: 消息列表（就地修改超长 tool 消息）。
        """
        for message in messages:
            if message.role != "tool":
                continue
            self._maybe_persist_tool_result(session, message)

    def _tool_age_seconds(self, message: Message) -> float:
        """计算工具结果年龄（Message.created_at 起算）。

        Args:
            message: 工具消息。

        Returns:
            年龄（秒）；时间戳缺失返回 0，视为新鲜不驱逐。
        """
        created = message.created_at
        if created is None:
            return 0.0
        return (datetime.now() - created).total_seconds()

    def _stale_result_reason(self, message: Message) -> str | None:
        """判断可重取工具结果是否过期。

        Args:
            message: 消息。

        Returns:
            过期占位文本；新鲜、豁免或未登记的工具返回 None。
        """
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
        """TTL 驱逐：过期工具结果替换为占位提示，由模型重新调用。

        只作用于可重复获得结果的工具：wiki 内容经 compile 变化后，
        长对话跨轮携带的旧读取结果不再准确。
        已替换的占位文本含"已过期"，再次 prepare 时跳过，不重复替换。

        Args:
            messages: 消息列表（就地修改过期 tool 消息）。
        """
        for message in messages:
            reason = self._stale_result_reason(message)
            if reason is None:
                continue
            # 已是占位文本则跳过，保证重复调用不二次替换
            if "已过期" in message.content:
                continue
            logger.info(
                "工具结果过期驱逐: %s（%s）",
                message.tool_name,
                _age_label(self._tool_age_seconds(message)),
            )
            message.content = reason

    def _repair_broken_history(self, messages: list[Message]):
        """修复断裂的历史——孤儿调用补占位 / 孤儿结果移除。

        Args:
            messages: 消息列表。

        Returns:
            修复后的消息列表。
        """
        messages = _repair_orphan_tool_call(messages=messages)
        messages = _remove_orphan_tool_result(messages=messages)
        return messages


# 历史修复工具（模块级函数）


def _get_orphan_tool_call_ids(messages: list[Message]) -> set[str]:
    """提取有调用无结果的 tool id（意外终止/截断造成）。

    Args:
        messages: 消息列表。

    Returns:
        孤儿调用 id 集合。
    """
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
    """提取无对应调用的工具结果下标（压缩/窗口截断造成）。

    Args:
        messages: 消息列表。

    Returns:
        孤儿结果的下标集合。
    """
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
    """为孤儿 tool_call 补占位 tool 消息——保证 API 请求合法。

    Args:
        messages: 消息列表。

    Returns:
        修复后的消息列表（无孤儿时原样返回）。
    """
    orphan_ids = _get_orphan_tool_call_ids(messages)
    if not orphan_ids:
        return messages

    repaired: list[Message] = []
    for m in messages:
        repaired.append(m)
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                if tc.id in orphan_ids:
                    repaired.append(
                        Message(
                            role="tool",
                            content="该工具执行中被意外打断，没有返回结果",
                            tool_call_id=tc.id,
                        )
                    )
    return repaired


def _remove_orphan_tool_result(messages: list[Message]) -> list[Message]:
    """移除无对应调用的工具结果。

    Args:
        messages: 消息列表。

    Returns:
        移除孤儿结果后的消息列表（无孤儿时原样返回）。
    """
    orphan_idx = _get_orphan_tool_result_idx(messages)
    if orphan_idx:
        return [m for idx, m in enumerate(messages) if idx not in orphan_idx]
    return messages
