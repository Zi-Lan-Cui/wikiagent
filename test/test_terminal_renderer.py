"""TerminalRenderer 渲染实体——流式缓冲/flush/收尾/工具行。

直接运行:  .venv/bin/python test/test_terminal_renderer.py
"""

import asyncio
import tempfile
from pathlib import Path

from rich.console import Console

from wiki_agent.events import RunContext
from wiki_agent.render.terminal_renderer import TerminalRenderer


def _renderer() -> TerminalRenderer:
    # file= 输出到临时文件——不让测试污染真实终端
    tmp = Path(tempfile.mkdtemp()) / "out.txt"
    return TerminalRenderer(Console(file=open(tmp, "w", encoding="utf-8")))


def _ctx() -> RunContext:
    return RunContext(session_key="t")


def test_flush_drains_buffer():
    r = _renderer()
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "第一部分"))
    asyncio.run(r.on_status(_ctx(), "compacting"))  # flush 触发
    # flush 后缓冲清空（未换行的第一部分已落持久输出）
    assert r._text == ""
    asyncio.run(r.on_stream_delta(_ctx(), "第二部分"))
    # 无换行增量留在缓冲等下一个换行/flush
    assert r._text == "第二部分"


def test_finish_leaves_content_in_console():
    """追加式：流式即最终——完整行即时落盘，无重渲染。

    旧设计收尾段经 capture + sys.stdout 重渲染（依赖 transient
    擦除消除预览帧），长回答下擦除失效会双渲染——追加式
    无预览、无擦除、无重渲染，架构上不存在双渲染。
    """
    tmp = Path(tempfile.mkdtemp()) / "out.txt"
    r = TerminalRenderer(Console(file=open(tmp, "w", encoding="utf-8")))
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "第一段\n"))  # 完整行即时打印
    asyncio.run(r.on_tool_call_start(_ctx(), "Grep", "c1", {"pattern": "x"}))  # flush 剩余缓冲
    asyncio.run(r.on_stream_delta(_ctx(), "第二段"))
    asyncio.run(r.on_run_end(_ctx()))  # 打印未闭合尾行

    # 收尾后缓冲清空
    assert r._text == ""
    # 两段都在 console file，且恰好各出现一次（无重渲染）
    out = tmp.read_text(encoding="utf-8")
    assert out.count("第一段") == 1
    assert out.count("第二段") == 1


def test_run_start_resets_state():
    r = _renderer()
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "旧内容\n"))
    asyncio.run(r.on_run_start(_ctx()))  # 新 turn 重置
    assert r._text == ""
    assert r._in_fence is False


def test_stream_delta_prints_complete_lines():
    """完整行即时打印，未闭合尾行留在缓冲。"""
    tmp = Path(tempfile.mkdtemp()) / "out.txt"
    r = TerminalRenderer(Console(file=open(tmp, "w", encoding="utf-8")))
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "第一行\n第二行"))  # 尾行无换行
    assert "第一行" in tmp.read_text(encoding="utf-8")
    assert "第二行" in r._text  # 未闭合，等 flush/finish
    asyncio.run(r.on_run_end(_ctx()))
    assert r._text == ""


def test_fence_state_machine():
    """``` 代码块整块缓冲，闭合时经 Markdown 渲染（标记隐藏 + 高亮）。"""
    tmp = Path(tempfile.mkdtemp()) / "out.txt"
    r = TerminalRenderer(Console(file=open(tmp, "w", encoding="utf-8")))
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "```python\nprint('a')\n```\n"))
    out = tmp.read_text(encoding="utf-8")
    assert "print('a')" in out  # 代码内容已渲染
    assert "```" not in out  # 块渲染隐藏 fence 标记
    assert r._in_fence is False  # 已闭合
    assert r._fence_buf == []  # 缓冲已清空


def test_unclosed_fence_flushed_as_block():
    """fence 未闭合就交错（工具行）——补假闭合行仍按代码块落盘。"""
    tmp = Path(tempfile.mkdtemp()) / "out.txt"
    r = TerminalRenderer(Console(file=open(tmp, "w", encoding="utf-8")))
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "```python\nprint('a')\n"))
    assert r._in_fence is True  # 未闭合
    asyncio.run(r.on_tool_call_start(_ctx(), "Grep", "c1", {"pattern": "x"}))  # flush 触发假闭合
    out = tmp.read_text(encoding="utf-8")
    assert "print('a')" in out
    assert r._in_fence is False
    assert r._fence_buf == []


def test_tool_result_timing_line():
    """工具行 + 结果行事件走渲染器不崩（输出到文件）。"""
    r = _renderer()
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_tool_call_start(_ctx(), "ReadFile", "c1", {"file_path": "index.md"}))
    asyncio.run(r.on_tool_result(_ctx(), "ReadFile", "c1", "文件内容\n第二行\n"))
    asyncio.run(r.on_tool_error(_ctx(), "Grep", "c2", "正则错误"))
    asyncio.run(r.on_run_end(_ctx()))


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
