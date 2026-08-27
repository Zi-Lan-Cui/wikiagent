"""wiki-agent CLI — 基于本地 Wiki 知识库的问答助手。

用法::

    VIRTUAL_ENV= .venv/bin/python cli.py
    uv run python cli.py
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from wiki_agent.agent import ReActAgent
from wiki_agent.config import load_config
from wiki_agent.errors import RetryableError
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.log.logger import configure_logging
from wiki_agent.render import TerminalRenderer
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry

HERE = Path(__file__).resolve().parent
console = Console()


# ════════════════════════════════════════════════════════════
#  视图组件
# ════════════════════════════════════════════════════════════


def _banner(llm: str) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column(style="dim")
    t.add_column(style="white")
    t.add_row("模型", llm)
    t.add_row("知识库", _wiki_overview())
    t.add_row("命令", "/help 帮助  /session 会话  /exit 退出")
    return Panel(t, title="Wiki Agent", border_style="cyan")


def _footer(elapsed: float, total_tokens: int, win: int = 0, cap: int = 0) -> Panel:
    t = Text()
    t.append(f"⏱ {elapsed:.1f}s", style="dim")
    t.append("  │  ", style="dim")
    if cap:
        pct = win / cap * 100
        color = "green" if pct < 50 else "yellow" if pct < 80 else "red"
        t.append(f"📊 {win:,} / {cap:,} ({pct:.0f}%)", style=color)
        t.append("  │  ", style="dim")
    t.append(f"🪙 {total_tokens:,}", style="dim")
    return Panel(t, border_style="cyan")


def _wiki_overview() -> str:
    parts = []
    wiki = HERE / "wiki"
    for d in ("concepts", "entities", "topics", "sources"):
        p = wiki / d
        if p.is_dir():
            n = sum(1 for _ in p.rglob("*.md"))
            if n:
                parts.append(f"[bold]{d}[/] [dim]{n}页[/]")
    idx = wiki / "index.md"
    if idx.exists():
        n = sum(1 for line in idx.read_text().split("\n") if line.startswith("- [["))
        parts.append(f"[bold]索引[/] [dim]{n}条[/]")
    return "  ".join(parts) if parts else "[red]wiki 为空[/]"


# ════════════════════════════════════════════════════════════
#  交互循环
# ════════════════════════════════════════════════════════════


def _new_session_key() -> str:
    """生成新 key——时间戳唯一，所有会话统一走这里。

    Returns:
        形如 session_YYYYMMDD_HHMMSS 的 key。
    """
    return f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


async def _interactive_loop(
    agent: ReActAgent, session_key: str, cli_hook: TerminalRenderer
) -> None:
    """交互循环——输入 → agent.run（渲染全走 hook）→ footer。"""
    console.print(_banner(agent.llm.model_id))
    console.print(f"  [bright_black]会话: {session_key}[/]\n")

    dream_task = None
    try:
        dream_task = asyncio.create_task(
            agent._dream_loop(interval=agent.agent_config.dream_interval)
        )

        while True:
            try:
                user_input = console.input("[bold green]你[/] [bold cyan]▶[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print(f"\n[bright_black]再见 👋  下次用 --resume {session_key} 继续[/]\n")
                break

            if not user_input:
                continue

            lo = user_input.lower()
            if lo in ("/q", "/exit", "/quit"):
                console.print(f"[bright_black]再见 👋  下次用 --resume {session_key} 继续[/]\n")
                break

            console.print()
            t0 = time.time()

            # 渲染全部走 hook 事件（TerminalRenderer 直接订阅）——
            # cli 只管编排，流式增量/工具行交错/收尾展示都在渲染器侧
            try:
                await agent.run(
                    user_input=user_input,
                    session_key=session_key,
                    stream=True,
                )
            except RetryableError as exc:
                # LLM 调用重试已耗尽（网络/限流）——提示后回到输入循环，
                # 不崩交互。本轮消息不落盘，重试时重新输入即可
                console.print(f"\n[red]调用失败（网络/限流）: {exc}[/]")
                console.print("[bright_black]本轮未保存，可重试或换个问法[/]")
            except Exception as exc:
                console.print(f"\n[red]本轮出错: {type(exc).__name__}: {exc}[/]")

            elapsed = time.time() - t0
            s = agent.session_manager.get_or_create(session_key)
            total_tk = s.token_cost.get("total", 0)
            console.print(
                _footer(
                    elapsed,
                    total_tk,
                    win=s.current_window_tokens,
                    cap=agent.agent_config.context_windows,
                )
            )

    finally:
        if dream_task:
            dream_task.cancel()


# ════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════


def main() -> None:
    """CLI 入口——解析参数 → 配置 → 组装 agent → 交互循环。"""
    import argparse

    parser = argparse.ArgumentParser(description="wiki-agent — 基于 wiki 知识库的问答助手")
    parser.add_argument("--resume", metavar="KEY", help="恢复指定会话（默认新建）")
    parser.add_argument("--list", action="store_true", help="列出所有历史会话后退出")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="全量日志 + 结构化事件写入 workspace/（debug.log + events.jsonl）",
    )
    args = parser.parse_args()

    # ── 配置: 单一入口，CLI 参数 > 环境变量 > .env > 默认值 ──
    cfg = load_config(project_root=HERE, overrides={"logging": {"debug": args.debug}})

    # ── 日志: 终端只显示 WARNING+；--debug 时全量写文件 ──
    workspace = cfg.paths.resolved_workspace_dir()
    configure_logging(
        file_path=str(workspace / "debug.log") if cfg.logging.debug else None,
    )
    if cfg.logging.debug:
        from wiki_agent.log import setup_event_log

        setup_event_log(workspace / "events.jsonl")

    llm = create_llm(cfg.llm)
    vlm = create_vlm(cfg.vlm)

    tool_registry = ToolRegistry()
    wiki = cfg.paths.resolved_wiki_dir()
    tool_registry.register(ReadFile(wiki, workspace=workspace))
    tool_registry.register(ListDir(wiki))
    tool_registry.register(Grep(wiki))

    cli_hook = TerminalRenderer(console)
    agent = ReActAgent(
        name="wiki-qa",
        llm=llm,
        vlm=vlm,
        tool_registry=tool_registry,
        workspace=workspace,
        wiki_dir=wiki,
        agent_config=cfg.agent,
        hooks=[cli_hook],
    )

    if args.list:
        keys = agent.session_manager.list_session_keys()
        if not keys:
            console.print("[bright_black]还没有历史会话[/]")
        else:
            console.print("[bold]历史会话[/]（时间倒序）:")
            for k in keys:
                s = agent.session_manager.get_or_create(k)
                console.print(f"  ○ {k} — {len(s.history)} 条消息")
        return

    session_key = args.resume or _new_session_key()
    asyncio.run(_run_cli(cfg, agent, session_key, cli_hook))


async def _run_cli(cfg, agent: ReActAgent, session_key: str, cli_hook: TerminalRenderer) -> None:
    """连接 MCP servers（如果配了）→ 交互循环 → 退出时清理连接。

    Args:
        cfg: 根配置（MCP 连接信息）。
        agent: ReActAgent 实例。
        session_key: 会话 key。
        cli_hook: 终端渲染器。
    """
    mcp_connections = {}
    if cfg.mcp.servers:
        from wiki_agent.tools.mcp_tools.mcp_adaptor import connect_mcp_servers

        # adaptor 吃 {name: McpServerConfig}——transport 在
        # cfg.transport 里，need_resources/need_prompts 是 server 级开关
        mcp_connections = await connect_mcp_servers(
            mcp_servers=cfg.mcp.servers,
            tool_registry=agent.tool_registry,
        )
        for name, conn in mcp_connections.items():
            console.print(f"  [bright_black]MCP 已连接: {name}[/]")

    try:
        await _interactive_loop(agent, session_key, cli_hook)
    finally:
        for name, conn in mcp_connections.items():
            await conn.aclose()
            console.print(f"  [bright_black]MCP 已断开: {name}[/]")


if __name__ == "__main__":
    main()
