"""终端渲染器——独立实体，直接作为 agent 的 hook 订阅事件并渲染。

单实体设计: 渲染器本身就是 hook（不再有适配器/独立流式状态机
两个中间层）。追加式流式输出——完整行即时渲染打印，增量即
最终；工具行渲染是它的内部逻辑——一个类、一份状态、一个入口。

事件 → 渲染映射::

    on_run_start        → 复位缓冲与 fence 状态
    on_stream_delta     → 缓冲累积 + 完整行即时打印
    on_status           → 打印剩余缓冲 + 状态提示行
    on_tool_call_start  → 打印剩余缓冲 + 工具行（时间序交错点）
    on_tool_result      → 结果行（✓ + 摘要 + 耗时）
    on_tool_error       → 错误行（✗）
    on_run_end          → 打印未闭合尾行

cli 用法::

    renderer = TerminalRenderer(console)
    agent = ReActAgent(..., hooks=[renderer])

为什么是追加式: 旧设计 transient Live（流式预览）+ 收尾
Markdown 重渲染，依赖擦除消除预览帧——长回答帧高超过终端
高度时 cursor-up 被 clamp，滚入 scrollback 的帧行擦不掉，
残留帧 + 重渲染 = 同一回答显示两遍。追加式没有预览、没有
擦除、没有重渲染——架构上不存在双渲染。代价: 多行 markdown
元素（表格/嵌套列表）按行渲染不如整篇渲染精致，可读性无损。
"""

from __future__ import annotations

import re
import time
from typing import Any

from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from wiki_agent.events import AgentHook, CommandProgress, RunContext

# 样式
TOOL_ICONS: dict[str, str] = {
    "ReadFile": "📖",
    "ListDir": "📂",
    "Grep": "🔍",
}
FALLBACK_ICON = "🔧"

START_STYLE = "bold yellow"
OK_STYLE = "green"
ERR_STYLE = "bold red"
DETAIL_STYLE = "dim"
ARG_STYLE = "bright_black"


# 工具行渲染辅助（纯函数）


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
        # fence 状态—— ``` 未闭合时代码行累积进 _fence_buf，
        # 闭合后整块交 Markdown 渲染（语法高亮 + 代码块样式），
        # 不逐行裸打印（逐行打印无高亮、fence 标记可见，观感
        # 像"代码块没渲染"）
        self._in_fence: bool = False
        self._fence_buf: list[str] = []
        self._timers: dict[str, float] = {}

    # 流式内部状态机（追加式——完整行即时打印）

    def _stream_delta(self, delta: str) -> None:
        """缓冲累积，完整行即时渲染打印。

        增量即最终——输出顺序天然正确，无预览帧、无擦除、无
        重渲染（Live 两步模型的长回答双渲染 bug 在此架构不存在）。

        Args:
            delta: 增量文本块。
        """
        self._text += delta
        # 只打印含换行的完整行；未闭合尾行留在缓冲等下一个
        # delta 或 flush/finish
        while "\n" in self._text:
            line, self._text = self._text.split("\n", 1)
            self._print_line(line)

    def _print_line(self, line: str) -> None:
        """渲染单行——fence 内累积，闭合时整块渲染；否则行级 Markdown。

        Args:
            line: 一行文本（不含换行符）。
        """
        stripped = line.strip()
        if self._in_fence:
            self._fence_buf.append(line)
            # 同字符三连（``` 或 ~~~）闭合 fence——整块渲染
            if stripped.startswith("```") or stripped.startswith("~~~"):
                self._close_fence()
            return
        if stripped.startswith("```") or stripped.startswith("~~~"):
            self._in_fence = True
            self._fence_buf = [line]
            return
        if line.strip():
            self._c.print(Markdown(line))
        else:
            self._c.print()

    def _close_fence(self) -> None:
        """fence 闭合——整块交 Markdown 渲染（语法高亮 + 代码块背景）。

        块级渲染只在闭合时发生一次——代码块生成期间的延迟
        （块不出现在屏幕上，直到闭合）换来的是一次性成型的
        代码块观感；LLM 代码块通常 < 20 行，延迟不可感知。
        """
        if self._fence_buf:
            self._c.print(Markdown("\n".join(self._fence_buf)))
        self._fence_buf = []
        self._in_fence = False

    def _flush_stream(self) -> None:
        """打印剩余缓冲并复位（工具行/状态行时间序交错点）。

        追加式下"冻结"简化为排空未闭合尾行——流式输出本来
        就按到达顺序落盘，交错只需保证尾行先于工具行打印。
        fence 未闭合就交错（模型消息中途被工具行打断）——
        补一个假闭合行让缓冲的代码块仍以代码块样式落盘。
        """
        if self._in_fence:
            self._fence_buf.append("```")
            self._close_fence()
        if self._text:
            self._print_line(self._text)
            self._text = ""

    def _finish_stream(self) -> None:
        """turn 收尾——打印未闭合尾行。

        无 Live、无重渲染——内容已在流式过程中全部落盘，
        收尾只剩最后一行未换行的缓冲。
        """
        self._flush_stream()

    # hook 事件（渲染入口）

    async def on_run_start(self, context: RunContext) -> None:
        # 复位缓冲与 fence 状态（跨 turn 复用渲染器实例）
        self._text = ""
        self._in_fence = False
        self._fence_buf = []

    async def on_stream_delta(self, context: RunContext, delta: str) -> None:
        self._stream_delta(delta)

    async def on_status(self, context: RunContext, status: str) -> None:
        labels = {"compacting": "  🗜️  压缩中..."}
        if text := labels.get(status):
            self._flush_stream()
            self._c.print(Text(text, style="dim"))

    async def on_command_start(
        self,
        context: RunContext,
        command: str,
        task_id: str,
    ) -> None:
        self._flush_stream()
        self._c.print(Text(f"  ▶ /{command} 开始（{task_id}）", style=START_STYLE))

    async def on_command_progress(
        self,
        context: RunContext,
        progress: CommandProgress,
    ) -> None:
        self._flush_stream()
        if progress.current is not None and progress.total:
            position = f"{progress.current}/{progress.total}"
        elif progress.current is not None:
            position = str(progress.current)
        else:
            position = ""
        suffix = f" {position}" if position else ""
        message = f"  · [{progress.stage}]{suffix}"
        if progress.message:
            message += f" {progress.message}"
        style = ERR_STYLE if progress.level == "error" else DETAIL_STYLE
        self._c.print(Text(message, style=style))

    async def on_command_end(
        self,
        context: RunContext,
        command: str,
        task_id: str,
        result: Any,
    ) -> None:
        self._flush_stream()
        status = getattr(result, "status", "succeeded") if result else "succeeded"
        style = ERR_STYLE if status == "failed" else OK_STYLE
        self._c.print(Text(f"  ✓ /{command} {status}（{task_id}）", style=style))

    async def on_command_error(
        self,
        context: RunContext,
        command: str,
        task_id: str,
        error: Any,
    ) -> None:
        self._flush_stream()
        self._c.print(
            Text(f"  ✗ /{command} 失败：{str(error)[:120]}（{task_id}）", style=ERR_STYLE)
        )

    async def on_command_cancelled(
        self,
        context: RunContext,
        command: str,
        task_id: str,
    ) -> None:
        self._flush_stream()
        self._c.print(Text(f"  ! /{command} 已取消（{task_id}）", style=ERR_STYLE))

    async def on_tool_call_start(
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
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
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        result: Any,
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
        self,
        context: RunContext,
        tool_name: str,
        tool_call_id: str,
        error: Any,
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
