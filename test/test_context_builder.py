"""ContextBuilder build 扩展——system prompt 五块组装。

直接运行:  .venv/bin/python test/test_context_builder.py
"""

import tempfile
from pathlib import Path

from wiki_agent.context import ContextBuilder
from wiki_agent.memory import MemoryStore
from wiki_agent.message import Message
from wiki_agent.session import Session
from wiki_agent.tools import ToolRegistry


_TEMPLATE = (
    "画像:{user_description}\n环境:{wiki_context}\n纠错:{corrections}\n"
    "工具:{tools_description}\n摘要:{summery}"
)


def _make_env(tmp: Path) -> tuple[ContextBuilder, Path]:
    """构造迷你 wiki 环境 + builder。"""
    wiki = tmp / "wiki"
    wiki.mkdir()
    (wiki / "purpose.md").write_text("个人 Python 知识库", encoding="utf-8")
    (wiki / "schema.md").write_text("concepts/entities/topics", encoding="utf-8")
    (wiki / "index.md").write_text(
        "- [[concepts/lambda]] — Lambda 表达式\n"
        "- [[entities/functools-wraps]] — wraps 装饰器\n",
        encoding="utf-8",
    )
    store = MemoryStore(workspace=tmp)
    builder = ContextBuilder(
        system_prompt=_TEMPLATE,
        tool_registery=ToolRegistry(),
        memory_store=store,
        wiki_dir=wiki,
    )
    return builder, wiki


def _build(builder: ContextBuilder) -> str:
    msgs = builder.build_messages(
        session=Session("t"),
        current_message=Message(role="user", content="你好"),
        last_summery="",
        history=[],
    )
    return msgs[0].content


def test_build_includes_wiki_context():
    tmp = Path(tempfile.mkdtemp())
    builder, _ = _make_env(tmp)
    sp = _build(builder)
    assert "个人 Python 知识库" in sp       # purpose
    assert "concepts/entities/topics" in sp  # schema
    assert "[[concepts/lambda]]" in sp       # index 地图
    assert "[[entities/functools-wraps]]" in sp


def test_build_includes_corrections():
    tmp = Path(tempfile.mkdtemp())
    builder, _ = _make_env(tmp)
    builder.memory_store.append_correction(
        text="[concepts/lambda.md] 示例代码有误", session_key="s1")
    sp = _build(builder)
    assert "示例代码有误" in sp


def test_build_index_truncated_by_line():
    """index 超限按行截断——不切断行，提示用工具探索。"""
    tmp = Path(tempfile.mkdtemp())
    builder, wiki = _make_env(tmp)
    long_line = "- [[concepts/x]] — " + "很长的描述" * 50
    (wiki / "index.md").write_text(
        "\n".join(long_line for _ in range(200)) + "\n", encoding="utf-8")
    sp = _build(builder)
    assert "地图过长已截断" in sp
    # 截断后每行完整（末行是完整条目）
    assert "很长的描述" in sp


def test_build_empty_wiki_graceful():
    """空库/无文件——占位文案不崩。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = tmp / "wiki"
    wiki.mkdir()
    builder = ContextBuilder(
        system_prompt=_TEMPLATE,
        tool_registery=ToolRegistry(),
        memory_store=MemoryStore(workspace=tmp),
        wiki_dir=wiki,
    )
    sp = _build(builder)
    assert "wiki 为空库" in sp or "尚无" in sp


def test_build_without_wiki_dir_graceful():
    """未传 wiki_dir——不崩，占位提示。"""
    tmp = Path(tempfile.mkdtemp())
    builder = ContextBuilder(
        system_prompt=_TEMPLATE,
        tool_registery=ToolRegistry(),
        memory_store=MemoryStore(workspace=tmp),
        wiki_dir=None,
    )
    sp = _build(builder)
    assert "未接入" in sp


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
