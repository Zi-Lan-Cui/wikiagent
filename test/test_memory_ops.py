"""MemoryOps 校验层测试——区间、判重、拒收理由、幂等语义。

直接运行:  .venv/bin/python test/test_memory_ops.py
"""

from pathlib import Path

import pytest

from wiki_agent.memory.ops import MemoryOps, OpRejected
from wiki_agent.memory.store import MemoryStore


def _ops(tmp: Path, batch=(0, 5)) -> tuple[MemoryOps, MemoryStore]:
    store = MemoryStore(workspace=tmp)
    return MemoryOps(store, batch_range=batch), store


def test_add_allocates_sequential_ids(tmp_path: Path):
    ops, store = _ops(tmp_path)
    assert ops.add("甲", "preference", "durable", ["s1"], [3]) == "m_0001"
    assert ops.add("乙", "knowledge", "ephemeral", ["s1"], [4]) == "m_0002"


def test_evidence_range_enforced(tmp_path: Path):
    """evidence 必须落在 (dream_cursor, history_cursor]——越界即伪造。"""
    ops, store = _ops(tmp_path)
    for bad in ([], [0], [6], [3, 99]):
        with pytest.raises(OpRejected):
            ops.add("丙", "preference", "durable", [], bad)
    assert ops.all_items() == []


def test_hash_dup_converts_to_confirm(tmp_path: Path):
    """完全相同文本不重复 add：转 confirm 加重证据并返回原 id。"""
    ops, store = _ops(tmp_path)
    first = ops.add("偏好深色  主题", "preference", "durable", ["s1"], [1])
    again = ops.add("偏好深色 主题", "preference", "durable", ["s1"], [2])
    assert again == first
    assert ops.all_items()[0].evidence == [1, 2]
    assert [event.event for event in store.read_events()] == ["add", "confirm"]


def test_unknown_and_expired_target_rejected(tmp_path: Path):
    ops, store = _ops(tmp_path)
    with pytest.raises(OpRejected, match="不存在"):
        ops.revise("m_9999", "文本")
    a = ops.add("临时", "task_state", "ephemeral", ["s1"], [1])
    ops.invalidate(a, None, "obsolete")
    ops.expire(a)
    with pytest.raises(OpRejected, match="expired_window_closed"):
        ops.reactivate(a, [2])
    with pytest.raises(OpRejected):
        ops.revise(a, "文本")


def test_invalidate_semantics(tmp_path: Path):
    ops, store = _ops(tmp_path)
    a = ops.add("旧事实", "project", "durable", ["s1"], [1])
    b = ops.add("新事实", "project", "durable", ["s1"], [2])
    with pytest.raises(OpRejected, match="bad_reason"):
        ops.invalidate(a, b, "because")
    ops.invalidate(a, b, "superseded")
    # 已失效再 invalidate：幂等 no-op，不落新事件
    n_before = len(store.read_events())
    ops.invalidate(a, b, "obsolete")
    assert len(store.read_events()) == n_before
    # by 指向失效条目 → 拒
    with pytest.raises(OpRejected, match="by_not_active"):
        ops.invalidate(b, a, "superseded")


def test_reactivate_noop_on_active(tmp_path: Path):
    ops, store = _ops(tmp_path)
    a = ops.add("有效", "preference", "durable", ["s1"], [1])
    n_before = len(store.read_events())
    ops.reactivate(a, [2])
    assert len(store.read_events()) == n_before
    assert all(i.status == "active" for i in ops.all_items())


if __name__ == "__main__":
    import tempfile

    root = Path(tempfile.mkdtemp())
    for i, (name, fn) in enumerate(
        [
            ("ids", test_add_allocates_sequential_ids),
            ("range", test_evidence_range_enforced),
            ("hashdup", test_hash_dup_converts_to_confirm),
            ("unknown", test_unknown_and_expired_target_rejected),
            ("invalidate", test_invalidate_semantics),
            ("reactivate", test_reactivate_noop_on_active),
        ]
    ):
        fn(root / f"ops-{i}")
        print("PASS", name)
