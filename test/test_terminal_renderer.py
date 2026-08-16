"""TerminalRenderer 渲染实体——流式缓冲/flush/收尾/工具行。

直接运行:  .venv/bin/python test/test_terminal_renderer.py
"""

import asyncio
import tempfile
from pathlib import Path

from rich.console import Console

from wiki_agent.hook.base import RunContext
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
    asyncio.run(r.on_stream_delta(_ctx(), "第二部分"))
    # flush 后缓冲只含第二部分（第一部分已落持久输出）
    assert "第一部分" not in r._text
    assert "第二部分" in r._text


def test_finish_renders_tail_segment_only():
    """收尾只渲染最后一次 flush 之后的片段——之前片段已在
    工具行交错时以纯文本落盘（console file），收尾段经
    capture + sys.stdout（绕过 console screen 状态的踩坑设计）。"""
    import io
    import sys as _sys

    tmp = Path(tempfile.mkdtemp()) / "out.txt"
    r = TerminalRenderer(Console(file=open(tmp, "w", encoding="utf-8")))
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "第一段"))
    asyncio.run(r.on_tool_call_start(
        _ctx(), "Grep", "c1", {"pattern": "x"}))   # flush 第一段落盘
    asyncio.run(r.on_stream_delta(_ctx(), "第二段"))

    captured = io.StringIO()
    old_stdout = _sys.stdout
    _sys.stdout = captured
    try:
        asyncio.run(r.on_run_end(_ctx()))
    finally:
        _sys.stdout = old_stdout

    # 收尾后缓冲清空
    assert r._text == ""
    # 第一段在 console file（flush 纯文本），第二段在 stdout（Markdown 收尾）
    assert "第一段" in tmp.read_text(encoding="utf-8")
    assert "第二段" in captured.getvalue()


def test_run_start_resets_state():
    r = _renderer()
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "旧内容"))
    asyncio.run(r.on_run_start(_ctx()))  # 新 turn 重置
    assert r._text == ""
    assert r._live is None


def test_stream_delta_starts_live_lazily():
    """空白增量不启动 Live；有内容才启动。"""
    r = _renderer()
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_stream_delta(_ctx(), "  "))  # 纯空白
    assert r._live is None
    asyncio.run(r.on_stream_delta(_ctx(), "有内容"))
    assert r._live is not None
    asyncio.run(r.on_run_end(_ctx()))


def test_tool_result_timing_line():
    """工具行 + 结果行事件走渲染器不崩（输出到文件）。"""
    r = _renderer()
    asyncio.run(r.on_run_start(_ctx()))
    asyncio.run(r.on_tool_call_start(
        _ctx(), "ReadFile", "c1", {"file_path": "index.md"}))
    asyncio.run(r.on_tool_result(
        _ctx(), "ReadFile", "c1", "文件内容\n第二行\n"))
    asyncio.run(r.on_tool_error(
        _ctx(), "Grep", "c2", "正则错误"))
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
