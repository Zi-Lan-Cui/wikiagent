"""统一异常队列——append/list/remove 与命令视图。

直接运行:  .venv/bin/python test/test_queue.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.queue import QueueStore


def test_append_list_remove_roundtrip():
    tmp = Path(tempfile.mkdtemp())
    store = QueueStore(tmp)
    store.append(
        "ingest_failure", source="compile", file="魔法方法.md", stage="plan", error="JSON 错误"
    )
    store.append("surgery_conflict", kind="merge", detail="两页冲突")

    items = store.list()
    assert len(items) == 2
    assert items[0]["type"] == "ingest_failure"
    assert items[0]["file"] == "魔法方法.md"
    assert items[0]["stage"] == "plan"

    # remove
    assert store.remove(items[0]["id"]) is True
    assert len(store.list()) == 1
    assert store.remove("nonexistent") is False


def test_queue_empty_file_missing():
    tmp = Path(tempfile.mkdtemp())
    store = QueueStore(tmp)
    assert store.list() == []
    assert store.get("x") is None


def test_count_by_type():
    tmp = Path(tempfile.mkdtemp())
    store = QueueStore(tmp)
    store.append("ingest_failure", source="compile", file="a")
    store.append("ingest_failure", source="refine", file="b")
    store.append("surgery_conflict", kind="merge")
    counts = store.count_by_type()
    assert counts == {"ingest_failure": 2, "surgery_conflict": 1}


def test_queue_command_view():
    """QueueCommand 列出 + done 移除。"""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from wiki_agent.command.commands import CommandContext, QueueCommand
    from wiki_agent.session import Session

    class _Agent:
        pass

    tmp = Path(tempfile.mkdtemp())
    from wiki_agent.memory import MemoryStore

    agent = _Agent()
    agent.workspace = tmp
    agent.memory_store = MemoryStore(workspace=tmp)

    store = QueueStore(tmp)
    store.append("ingest_failure", source="compile", file="x.md", stage="plan", error="JSON 错误")
    agent.memory_store.append_correction(text="[concepts/lambda.md] 示例有误")

    cmd = QueueCommand()
    session = Session("t")

    # 列表视图
    result = asyncio.run(
        cmd.execute(
            CommandContext(raw="/queue", key="queue", args="", session=session, agent=agent)
        )
    )
    assert "待处理" in result.text
    assert "ingest 失败" in result.text
    assert "示例有误" in result.text  # corrections 聚合

    # done 移除
    item_id = store.list()[0]["id"]
    result = asyncio.run(
        cmd.execute(
            CommandContext(
                raw="/queue done", key="queue", args=f"done {item_id}", session=session, agent=agent
            )
        )
    )
    assert "已移除" in result.text
    assert len(store.list()) == 0


def test_queue_command_empty():
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from wiki_agent.command.commands import CommandContext, QueueCommand
    from wiki_agent.session import Session

    class _Agent:
        pass

    tmp = Path(tempfile.mkdtemp())
    from wiki_agent.memory import MemoryStore

    agent = _Agent()
    agent.workspace = tmp
    agent.memory_store = MemoryStore(workspace=tmp)

    result = asyncio.run(
        QueueCommand().execute(
            CommandContext(raw="/queue", key="queue", args="", session=Session("t"), agent=agent)
        )
    )
    assert "队列为空" in result.text


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
