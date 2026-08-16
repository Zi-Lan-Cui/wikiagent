"""终端渲染器——独立实体，直接作为 agent 的 hook 订阅事件并渲染。

单实体设计: 渲染器本身就是 hook（不再有适配器/独立流式状态机
两个中间层）。流式缓冲 + Live 生命周期是它的内部状态，
工具行渲染是它的内部逻辑——一个类、一份状态、一个入口。

事件 → 渲染映射::

    on_run_start        → 重置流式缓冲
    on_stream_delta     → 缓冲累积 + Live 实时刷新
    on_status           → 冻结缓冲 + 状态提示行
    on_tool_call_start  → 冻结缓冲 + 工具行（时间序交错点）
    on_tool_result      → 结果行（✓ + 摘要 + 耗时）
    on_tool_error       → 错误行（✗）
    on_run_end          → 收尾: 停止 Live + Markdown 展示

cli 用法::

    renderer = TerminalRenderer(console)
    agent = ReActAgent(..., hooks=[renderer])

工具行打印前必须冻结流式缓冲——否则 transient Live 擦除会
打乱叙述文本与工具行的时间序。
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any

from rich.cells import cell_len
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from wiki_agent.hook.base import AgentHook, RunContext

# ── 样式 ──────────────────────────────────────────────────
TOOL_ICONS: dict[str, str] = {
    "ReadFile": "📖",
    "ListDir":  "📂",
    "Grep":     "🔍",
}
FALLBACK_ICON = "🔧"

START_STYLE = "bold yellow"
OK_STYLE = "green"
ERR_STYLE = "bold red"
DETAIL_STYLE = "dim"
ARG_STYLE = "bright_black"


# ── 工具行渲染辅助（纯函数）────────────────────────────────

def _trunc(s: str, max_len: int = 80) -> str:
    """截断长文本，按终端显示宽度（Rich cell_len——CJK 双宽/
    emoji 宽度由 Rich 统一处理，替代手写 Unicode 区间判断）。

    Args:
        s: 原始文本。
        max_len: 最大显示宽度。

    Returns:
        截断后的文本（尾部加 ...）。
    """
    if cell_len(s) <= max_len:
        return s
    acc = 0
    for i, ch in enumerate(s):
        acc += cell_len(ch)
        if acc > max_len - 3:
            return s[:i] + "..."
    return s


def _fmt_args(args: dict[str, Any]) -> str:
    """格式化工具参数为简短摘要。最多显示 2 个关键参数。

    Args:
        args: 工具参数字典。

    Returns:
        摘要文本（如 "(file_path=index.md)"）；无关键参数返回空串。
    """
    keys = _key_params(args)
    if not keys:
        return ""
    parts = []
    for k in keys[:2]:
        parts.append(f"{k}={_trunc(str(args[k]), 40)}")
    return "(" + ", ".join(parts) + ")"


def _key_params(args: dict[str, Any]) -> list[str]:
    """提取最关键的参数名，优先级靠前的排在前。

    Args:
        args: 工具参数字典。

    Returns:
        参数名列表。
    """
    order = ["file_path", "dir_path", "pattern", "query", "collection_name"]
    keys = [k for k in order if k in args]
    keys += [k for k in args if k not in keys]
    return keys


def _result_summary(tool_name: str, result: str) -> str:
    """从工具结果中提取摘要（按工具类型）。

    Args:
        tool_name: 工具名。
        result: 工具结果文本。

    Returns:
        摘要文本。
    """
    result = result.strip()
    if tool_name == "ReadFile":
        lines = result.count("\n") + 1 if result else 0
        chars = len(result)
        return f"{lines} 行, {chars:,} 字符"
    elif tool_name == "Grep":
        first_line = result.split("\n")[0] if result else ""
        m = re.search(r"找到\s*(\d+)\s*条", first_line)
        if m:
            return f"{m.group(1)} 条匹配"
        return f"{len(result.splitlines())} 行"
    elif tool_name == "ListDir":
        first_line = result.split("\n")[0] if result else ""
        return first_line.replace("# ", "")
    else:
        return _trunc(result, 60)


# ════════════════════════════════════════════════════════════
#  TerminalRenderer — 渲染实体（直接订阅 hook 事件）
# ════════════════════════════════════════════════════════════

class TerminalRenderer(AgentHook):
    """终端渲染实体——流式文本 + 工具行的全部渲染状态与逻辑。"""

    def __init__(
        self,
        console: Console,
        *,
        show_start: bool = True,
        show_result: bool = True,
        show_timing: bool = True,
    ):
        super().__init__()
        self._c = console
        self._show_start = show_start
        self._show_result = show_result
        self._show_timing = show_timing
        # 流式状态（内部——不再是独立实体）。
        # _text 字符串累积而非 list——_stream_delta 每个 token 触发一次，
        # list+join 是 O(n²)（长回复每 delta 全量 join），+= 是 O(1) 摊销
        self._text: str = ""
        self._live: Live | None = None
        self._timers: dict[str, float] = {}

    # ── 流式内部状态机 ────────────────────────────────────

    def _stream_delta(self, delta: str) -> None:
        """缓冲累积 + Live 惰性启动并刷新。

        Args:
            delta: 增量文本块。
        """
        self._text += delta
        if not self._text.strip():
            return
        if self._live is None:
            self._live = Live(
                Text(""), console=self._c,
                auto_refresh=False, transient=True,
            )
            self._live.start()
        self._live.update(Text(self._text))
        self._live.refresh()

    def _flush_stream(self) -> None:
        """把当前流式缓冲冻结成持久输出（工具行时间序交错点）。"""
        if self._live is not None:
            self._live.stop()   # transient 自动擦除局部帧
            self._live = None
        text = self._text
        self._text = ""
        if text.strip():
            self._c.print(text)

    def _finish_stream(self) -> str:
        """turn 收尾——停止 Live + Markdown 展示。

        Returns:
            尾部片段（最后一次 flush 之后的文本——之前片段已在
            工具行交错时以纯文本落盘，重复渲染会重复显示）。
        """
        if self._live is not None:
            self._live.stop()
            self._live = None
        full = self._text
        self._text = ""
        if not full.strip():
            return full
        # 不用 self._c.print(Markdown(full))——transient Live stop 后
        # console 仍处于 screen 清理状态，直接 print 会与 Live 的
        # 擦除序列交互（Markdown 输出被清屏序列吞掉/错位）。
        # capture 隔离渲染拿到成品字节，绕过 console 的 screen
        # 状态直接写 stdout。这是踩过的坑——不要"简化"。
        with self._c.capture() as cap:
            self._c.print(Markdown(full))
        sys.stdout.write(cap.get())
        sys.stdout.flush()
        return full

    # ── hook 事件（渲染入口）──────────────────────────────

    async def on_run_start(self, context: RunContext) -> None:
        self._text = ""
        self._live = None

    async def on_stream_delta(self, context: RunContext, delta: str) -> None:
        self._stream_delta(delta)

    async def on_status(self, context: RunContext, status: str) -> None:
        labels = {"compacting": "  🗜️  压缩中..."}
        if text := labels.get(status):
            self._flush_stream()
            self._c.print(Text(text, style="dim"))

    async def on_tool_call_start(
        self, context: RunContext,
        tool_name: str, tool_call_id: str, arguments: dict[str, Any],
    ) -> None:
        self._flush_stream()
        self._timers[tool_call_id] = time.time()

        icon = TOOL_ICONS.get(tool_name, FALLBACK_ICON)
        args_str = _fmt_args(arguments)
        line = Text()
        line.append(f"  {icon} ", style="")
        line.append(tool_name, style=START_STYLE)
        if args_str:
            line.append(f" {args_str}", style=ARG_STYLE)
        self._c.print(line)

    async def on_tool_result(
        self, context: RunContext,
        tool_name: str, tool_call_id: str, result: Any,
    ) -> None:
        elapsed = time.time() - self._timers.pop(tool_call_id, 0)

        result_str = str(result) if not isinstance(result, str) else result
        summary = _result_summary(tool_name, result_str)

        line = Text()
        line.append("    ", style="")
        line.append("✓", style=OK_STYLE)
        line.append(f" {summary}", style=DETAIL_STYLE)
        if self._show_timing and elapsed > 0.05:
            line.append(f" ({elapsed:.1f}s)", style=ARG_STYLE)
        self._c.print(line)

    async def on_tool_error(
        self, context: RunContext,
        tool_name: str, tool_call_id: str, error: Any,
    ) -> None:
        self._timers.pop(tool_call_id, None)

        err_str = str(error) if not isinstance(error, str) else error
        line = Text()
        line.append("    ", style="")
        line.append("✗", style=ERR_STYLE)
        line.append(f" {_trunc(err_str, 80)}", style=ERR_STYLE)
        self._c.print(line)

    async def on_run_end(self, context: RunContext) -> None:
        self._finish_stream()
