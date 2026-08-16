"""ContextGovernor 回归——snip 顺序修复 + 合并/修复行为。

直接运行:  .venv/bin/python test/test_governor.py
"""

import tempfile
from pathlib import Path

from wiki_agent.context.context_governor import ContextGovernor
from wiki_agent.message import Message, ToolCall
from wiki_agent.session import Session


def _gov() -> ContextGovernor:
    return ContextGovernor(workspace=Path(tempfile.mkdtemp()))




def _cfg(ctx: int, mt: int):
    """测试用 AgentConfig 对象——governor 签名统一为 agent_config。"""
    from wiki_agent.config import AgentConfig
    return AgentConfig(context_windows=ctx, max_tokens=mt)


def _long_msgs(n_rounds: int) -> list[Message]:
    """构造 n 轮纯文本对话（每轮 user+assistant，内容足够长）。"""
    msgs = [Message(role="system", content="SYS")]
    for i in range(n_rounds):
        msgs.append(Message(role="user", content=f"第{i}个问题 " + "内容" * 100))
        msgs.append(Message(role="assistant", content=f"第{i}个回答 " + "内容" * 100))
    return msgs


def test_snip_preserves_chronological_order():
    """A1 回归: snip 后的消息保持时间顺序（旧代码逆序输出）。"""
    gov = _gov()
    msgs = _long_msgs(40)
    out = gov._snip_by_tokens(msgs, _cfg(20000, 2000))
    # 输出必须严格时间递增：问题序号单调不减
    idxs = []
    for m in out:
        if m.role == "user" and m.content.startswith("第"):
            idxs.append(int(m.content[1:].split("个")[0]))
    assert idxs == sorted(idxs), f"顺序错乱: {idxs[:10]}..."
    assert out[0].role == "system"
    # 结尾是最近的轮次（不是最老的）
    assert "39" in out[-1].content or "38" in out[-1].content


def test_snip_starts_with_user():
    """snip 结果的起始必须合法（user 开头）。"""
    gov = _gov()
    msgs = _long_msgs(40)
    out = gov._snip_by_tokens(msgs, _cfg(20000, 2000))
    assert out[1].role == "user", f"第二条应是 user，实际 {out[1].role}"


def test_snip_budget_untouched_returns_all():
    """预算充足时不截断。"""
    gov = _gov()
    msgs = _long_msgs(2)
    out = gov._snip_by_tokens(msgs, _cfg(100000, 1000))
    assert len(out) == len(msgs)


def test_merge_consecutive_same_role():
    """连续同 role 合并，不合并带 tool_calls 的。"""
    gov = _gov()
    msgs = [
        Message(role="user", content="a"),
        Message(role="user", content="b"),
        Message(role="assistant", content="c"),
        Message(role="assistant", content="d"),
        Message(role="tool", content="t1", tool_call_id="x"),
        Message(role="tool", content="t2", tool_call_id="x"),
    ]
    merged = gov._merge_consecutive(msgs)
    assert [m.role for m in merged] == ["user", "assistant", "tool", "tool"]
    assert merged[0].content == "a\n\nb"


def test_repair_orphan_tool_call_fills_placeholder():
    """孤儿 tool_call（无结果）补占位 tool 消息，保证 API 合法。"""
    gov = _gov()
    msgs = [
        Message(role="user", content="q"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="call_1", name="Grep", arguments={}),
        ]),
        Message(role="user", content="q2"),
    ]
    repaired = gov._repair_broken_history(msgs)
    roles = [m.role for m in repaired]
    assert "tool" in roles
    assert "意外打断" in [m.content for m in repaired if m.role == "tool"][0]


def test_repair_orphan_tool_result_removed():
    """孤儿 tool 结果（无对应调用）被移除。"""
    gov = _gov()
    msgs = [
        Message(role="user", content="q"),
        Message(role="tool", content="孤儿结果", tool_call_id="ghost"),
        Message(role="user", content="q2"),
    ]
    repaired = gov._repair_broken_history(msgs)
    assert all(not (m.role == "tool" and m.tool_call_id == "ghost") for m in repaired)


# ════════════════════════════════════════════════════════════
#  工具结果 TTL 驱逐
# ════════════════════════════════════════════════════════════

from datetime import datetime


def _old_tool_message(name: str, age_seconds: float, content: str = "旧结果") -> Message:
    old_ts = (datetime.now().timestamp() - age_seconds)
    # metadata 构造时传参（pydantic 验证 dict → MessageMeta）；
    # 构造后赋值不触发验证，直接放 dict 会在 created_at 读取时崩
    return Message(
        role="tool", tool_name=name, tool_call_id="c1", content=content,
        metadata={"time_stamp": datetime.fromtimestamp(old_ts).isoformat()},
    )


def _fresh_tool_message(name: str, content: str = "新结果") -> Message:
    return Message(role="tool", tool_name=name, tool_call_id="c2",
                   content=content)


def test_stale_tool_result_expired():
    """可重复获得工具超 TTL → 内容清除 + 重调提示。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 60}   # 1 分钟 TTL 便于测试
    msgs = [_old_tool_message("ReadFile", age_seconds=120)]
    gov._expire_stale_tool_results(msgs)
    assert "已过期" in msgs[0].content
    assert "ReadFile" in msgs[0].content
    assert "旧结果" not in msgs[0].content


def test_fresh_tool_result_kept():
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 3600}
    msgs = [_fresh_tool_message("ReadFile")]
    gov._expire_stale_tool_results(msgs)
    assert msgs[0].content == "新结果"


def test_unregistered_tool_not_expired():
    """未登记 TTL 的工具（如 MCP）不驱逐——不可重复获得的保守保留。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 60}
    msgs = [_old_tool_message("weather_search", age_seconds=9999)]
    gov._expire_stale_tool_results(msgs)
    assert msgs[0].content == "旧结果"


def test_internal_transient_tool_exempt():
    """RecordCorrection 等瞬时工具豁免——结果无时效内容。"""
    gov = _gov()
    gov._tool_ttl = {"RecordCorrection": 60}
    msgs = [_old_tool_message("RecordCorrection", age_seconds=9999)]
    gov._expire_stale_tool_results(msgs)
    assert msgs[0].content == "旧结果"


def test_expire_idempotent():
    """已驱逐的占位消息不二次替换（防死循环）。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 60}
    msgs = [_old_tool_message("ReadFile", age_seconds=120)]
    gov._expire_stale_tool_results(msgs)
    first = msgs[0].content
    gov._expire_stale_tool_results(msgs)
    assert msgs[0].content == first


def test_prepare_for_llm_includes_expiry():
    """prepare_for_llm 管道含驱逐步骤（端到端）。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 60}
    msgs = [
        Message(role="system", content="SYS"),
        Message(role="user", content="问"),
        # 带真实 tool_calls 的 assistant——工具结果不是孤儿（repair 不删）
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="c1", name="ReadFile", arguments={}),
        ]),
        _old_tool_message("ReadFile", age_seconds=120),
    ]
    out = gov.prepare_for_llm(Session("t"), msgs,
                              _cfg(100000, 1000))
    tool_msgs = [m for m in out if m.role == "tool"]
    assert tool_msgs and "已过期" in tool_msgs[0].content


# ════════════════════════════════════════════════════════════
#  窗口维度紧凑化（inflight overflow）
# ════════════════════════════════════════════════════════════


def _big_tool_message(name: str, chars: int = 5000, call_id: str = "c9") -> Message:
    return Message(role="tool", tool_name=name, tool_call_id=call_id,
                   content="数据" * chars)


def test_inflight_overflow_compacts_big_tool_result():
    """snip 后仍超预算 → 可重取工具结果换占位符。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 3600}
    # 预算极小：单条巨型工具结果必超
    msgs = [
        Message(role="system", content="SYS"),
        Message(role="user", content="问"),
        _big_tool_message("ReadFile"),
    ]
    gov._compact_inflight_overflow(
        msgs, _cfg(3000, 500))
    assert "已紧凑化" in msgs[2].content
    assert "数据" not in msgs[2].content


def test_inflight_within_budget_no_change():
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 3600}
    msgs = [
        Message(role="system", content="SYS"),
        Message(role="user", content="问"),
        _big_tool_message("ReadFile", chars=100),
    ]
    gov._compact_inflight_overflow(
        msgs, _cfg(100000, 1000))
    assert "已紧凑化" not in msgs[2].content


def test_inflight_unregistered_tool_untouched():
    """未登记（不可重取）工具不紧凑化——内容丢了就真没了。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 3600}
    msgs = [
        Message(role="system", content="SYS"),
        _big_tool_message("weather_search", chars=5000),
    ]
    gov._compact_inflight_overflow(
        msgs, _cfg(3000, 500))
    assert "已紧凑化" not in msgs[1].content


def test_inflight_short_result_untouched():
    """短结果（<500 字符）不紧凑化——省不了多少，保留完整信息。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 3600}
    msgs = [
        Message(role="system", content="SYS"),
        _big_tool_message("ReadFile", chars=100),
    ]
    gov._compact_inflight_overflow(
        msgs, _cfg(3000, 500))
    assert "已紧凑化" not in msgs[1].content


def test_inflight_keeps_newest_result():
    """最新一条工具结果保留——最新的最可能有价值。"""
    gov = _gov()
    gov._tool_ttl = {"Grep": 3600}
    msgs = [
        Message(role="system", content="SYS"),
        _big_tool_message("Grep", call_id="old"),
        _big_tool_message("Grep", call_id="new"),
    ]
    gov._compact_inflight_overflow(
        msgs, _cfg(3000, 500))
    # 旧的结果被紧凑化，最新的保留
    assert "已紧凑化" in msgs[1].content
    assert "已紧凑化" not in msgs[2].content


def test_inflight_idempotent():
    """已紧凑化的不二次替换。"""
    gov = _gov()
    gov._tool_ttl = {"ReadFile": 3600}
    msgs = [
        Message(role="system", content="SYS"),
        _big_tool_message("ReadFile"),
    ]
    gov._compact_inflight_overflow(
        msgs, _cfg(3000, 500))
    first = msgs[1].content
    gov._compact_inflight_overflow(
        msgs, _cfg(3000, 500))
    assert msgs[1].content == first


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
