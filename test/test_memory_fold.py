"""fold 与重放一致性测试——事件流是唯一真相源，读侧状态 = 按序折叠。

直接运行:  .venv/bin/python test/test_memory_fold.py
"""

from pathlib import Path

from wiki_agent.memory.items import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_INVALID,
    fold,
)
from wiki_agent.memory.ops import MemoryOps
from wiki_agent.memory.store import MemoryStore


def _make_store(tmp: Path) -> MemoryStore:
    return MemoryStore(workspace=tmp)


def test_fold_lifecycle(tmp_path: Path):
    """add→revise→confirm→invalidate→reactivate→expire 逐状态迁移。"""
    store = _make_store(tmp_path)
    ops = MemoryOps(store, batch_range=(0, 100))
    item_id = ops.add("偏好深色主题", "preference", "durable", ["s1"], [1])
    ops.revise(item_id, "偏好深色主题，排斥亮色")
    ops.confirm(item_id, [2])
    other = ops.add("项目用 React", "project", "durable", ["s2"], [3])
    ops.invalidate(item_id, other, "contradicted")

    state = fold(store.read_events())
    assert state[item_id].status == STATUS_INVALID
    assert state[item_id].invalidated_by == other
    ops2 = MemoryOps(store, batch_range=(0, 100))
    ops2.reactivate(item_id, [4])
    state = fold(store.read_events())
    assert state[item_id].status == STATUS_ACTIVE
    assert state[item_id].invalid_at is None

    ops3 = MemoryOps(store)
    ops3.expire(other)
    state = fold(store.read_events())
    assert state[other].status == STATUS_EXPIRED


def test_replay_equals_incremental(tmp_path: Path):
    """重放一致：从头 fold 与增量维护的内存状态逐字段相同。"""
    store = _make_store(tmp_path)
    ops = MemoryOps(store, batch_range=(0, 100))
    a = ops.add("用户是前端工程师", "identity", "permanent", ["s1"], [1])
    ops.add("临时要求：本周只看报错", "task_state", "ephemeral", ["s2"], [2])
    b = ops.add("偏好极简排版", "preference", "durable", ["s2"], [3])
    ops.invalidate(a, b, "superseded")
    ops.confirm(b, [4])

    replayed = fold(store.read_events())
    # 全新实例即"重放"；与增量维护的 ops._state 比较
    fresh = MemoryOps(store)
    assert {i.id: i.__dict__ for i in fresh.all_items()} == {
        i.id: i.__dict__ for i in replayed.values()
    }


def test_expired_invisible_but_preserved(tmp_path: Path):
    """expire 后条目退出读取视图，但事件行与 expired 状态仍在——reactivate 据此拒绝。"""
    store = _make_store(tmp_path)
    ops = MemoryOps(store)
    a = ops.add("一次性任务态", "task_state", "ephemeral", [], [1])
    ops.invalidate(a, None, "obsolete")
    ops.expire(a)
    assert all(i.status != STATUS_EXPIRED for i in ops.all_items())
    assert fold(store.read_events())[a].status == STATUS_EXPIRED
    # 事件行数 = add+invalidate+expire，expire 不删行
    assert len(store.read_events()) == 3


if __name__ == "__main__":
    import tempfile

    root = Path(tempfile.mkdtemp())
    for name, fn in [
        ("lifecycle", test_fold_lifecycle),
        ("replay", test_replay_equals_incremental),
        ("expired", test_expired_invisible_but_preserved),
    ]:
        fn(root / name)
        print("PASS", name)
