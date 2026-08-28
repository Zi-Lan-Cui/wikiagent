"""纠错管道——独立于画像管道（不做 LLM 加工，自然语言原样落账）。

直接运行:  .venv/bin/python test/test_corrections.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.memory import MemoryStore
from wiki_agent.tools import RecordCorrection, ToolRegistry


def test_append_and_read_corrections():
    tmp = Path(tempfile.mkdtemp())
    store = MemoryStore(workspace=tmp)
    store.append_correction(text="闭包捕获示例有误", session_key="s1")
    store.append_correction(text="生成器页缺 yield from 部分")

    items = store.get_corrections()
    assert len(items) == 2
    assert "闭包捕获示例有误" in items[0]
    assert "[s1]" in items[0]  # 带 session 标记
    assert "yield from" in items[1]  # 第二条无 session 标记


def test_corrections_file_separate_from_memory():
    """corrections.md 与 memory.md 分文件——画像管道不受污染。"""
    tmp = Path(tempfile.mkdtemp())
    store = MemoryStore(workspace=tmp)
    store.append_correction(text="X 页有误")
    assert store.corrections_file.exists()
    assert "X 页有误" not in store.get_memory_text()  # 画像里没有


def test_empty_corrections_returns_empty_list():
    tmp = Path(tempfile.mkdtemp())
    store = MemoryStore(workspace=tmp)
    assert store.get_corrections() == []


def test_record_correction_tool_executes():
    tmp = Path(tempfile.mkdtemp())
    store = MemoryStore(workspace=tmp)
    tool = RecordCorrection(store)

    registry = ToolRegistry()
    registry.register(tool)
    out = asyncio.run(
        registry.execute(tool.name, {"page": "concepts/lambda.md", "issue": "示例代码缩进错误"})
    )
    assert "已记录" in out
    items = store.get_corrections()
    assert len(items) == 1
    assert "concepts/lambda.md" in items[0]
    assert "示例代码缩进错误" in items[0]


def test_record_correction_tool_empty_issue():
    tmp = Path(tempfile.mkdtemp())
    store = MemoryStore(workspace=tmp)
    tool = RecordCorrection(store)
    registry = ToolRegistry()
    registry.register(tool)
    out = asyncio.run(registry.execute(tool.name, {"issue": ""}))
    assert "未记录" in out
    assert store.get_corrections() == []


def test_resolve_command_flow():
    """/resolve 裁决流——列出/accept/reject/keep 三态。"""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from wiki_agent.command.commands import CommandContext, ResolveCommand
    from wiki_agent.session import Session

    class _Agent:
        pass

    tmp = Path(tempfile.mkdtemp())
    agent = _Agent()
    agent.memory_store = MemoryStore(workspace=tmp)
    agent.memory_store.append_correction(text="[concepts/a.md] 说法有误")
    agent.memory_store.append_correction(text="[concepts/b.md] 缺示例")

    cmd = ResolveCommand()
    session = Session("t")

    # 列表
    r = asyncio.run(
        cmd.execute(
            CommandContext(raw="/resolve", key="resolve", args="", session=session, agent=agent)
        )
    )
    assert "1." in r.text and "concepts/a.md" in r.text
    assert "2." in r.text

    # accept 第 1 条
    r = asyncio.run(
        cmd.execute(
            CommandContext(
                raw="/resolve accept 1",
                key="resolve",
                args="accept 1",
                session=session,
                agent=agent,
            )
        )
    )
    assert "已确认待修" in r.text
    items = agent.memory_store.get_corrections()
    assert "[已确认待修]" in items[0]

    # reject 第 2 条
    r = asyncio.run(
        cmd.execute(
            CommandContext(
                raw="/resolve reject 2",
                key="resolve",
                args="reject 2",
                session=session,
                agent=agent,
            )
        )
    )
    assert "已驳回" in r.text
    items = agent.memory_store.get_corrections()
    assert len(items) == 1 and "concepts/b.md" not in items[0]

    # keep 剩余那条
    r = asyncio.run(
        cmd.execute(
            CommandContext(
                raw="/resolve keep 1", key="resolve", args="keep 1", session=session, agent=agent
            )
        )
    )
    assert "存疑" in r.text
    assert "[存疑]" in agent.memory_store.get_corrections()[0]


def test_resolve_invalid_index():
    """序号无效——不崩。"""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from wiki_agent.command.commands import CommandContext, ResolveCommand
    from wiki_agent.session import Session

    class _Agent:
        pass

    tmp = Path(tempfile.mkdtemp())
    agent = _Agent()
    agent.memory_store = MemoryStore(workspace=tmp)
    cmd = ResolveCommand()
    r = asyncio.run(
        cmd.execute(
            CommandContext(
                raw="/resolve reject 99",
                key="resolve",
                args="reject 99",
                session=Session("t"),
                agent=agent,
            )
        )
    )
    assert "序号无效" in r.text


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
