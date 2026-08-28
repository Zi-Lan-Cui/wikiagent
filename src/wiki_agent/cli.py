"""Installed command-line entry point for the local Wiki assistant."""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from wiki_agent.agent import ReActAgent
from wiki_agent.config import load_config
from wiki_agent.errors import RetryableError
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.log.logger import configure_logging
from wiki_agent.render import TerminalRenderer
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry

console = Console()


def _new_session_key() -> str:
    return f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _footer(elapsed: float, total_tokens: int, win: int, cap: int) -> Panel:
    text = Text(f"⏱ {elapsed:.1f}s  │  🪙 {total_tokens:,}", style="dim")
    if cap:
        text.append(f"  │  📊 {win:,} / {cap:,}")
    return Panel(text, border_style="cyan")


async def _interactive_loop(agent: ReActAgent, session_key: str) -> None:
    console.print(Panel(f"模型: {agent.llm.model_id}\n会话: {session_key}", title="Wiki Agent"))
    dream_task = asyncio.create_task(agent._dream_loop(interval=agent.agent_config.dream_interval))
    try:
        while True:
            try:
                user_input = console.input("[bold green]你[/] [bold cyan]▶[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                continue
            if user_input.lower() in {"/q", "/quit", "/exit"}:
                break
            started = time.monotonic()
            try:
                await agent.run(user_input=user_input, session_key=session_key, stream=True)
            except RetryableError as exc:
                console.print(f"[red]调用失败（网络/限流）: {exc}[/]")
            except Exception as exc:
                console.print(f"[red]本轮出错: {type(exc).__name__}: {exc}[/]")
            session = agent.session_manager.get_or_create(session_key)
            console.print(
                _footer(
                    time.monotonic() - started,
                    session.token_cost.get("total", 0),
                    session.current_window_tokens,
                    agent.agent_config.context_windows,
                )
            )
    finally:
        dream_task.cancel()
        await asyncio.gather(dream_task, return_exceptions=True)


async def _run(agent: ReActAgent, cfg, session_key: str) -> None:
    connections = {}
    if cfg.mcp.servers:
        from wiki_agent.tools.mcp_tools.mcp_adaptor import connect_mcp_servers

        connections = await connect_mcp_servers(cfg.mcp.servers, agent.tool_registry)
    try:
        await _interactive_loop(agent, session_key)
    finally:
        for connection in connections.values():
            await connection.aclose()


def main() -> None:
    """Launch the installed ``wiki-agent`` command."""
    parser = argparse.ArgumentParser(description="wiki-agent — 基于 wiki 知识库的问答助手")
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(), help="项目根目录（默认当前目录）"
    )
    parser.add_argument("--resume", metavar="KEY", help="恢复指定会话")
    parser.add_argument("--list", action="store_true", help="列出历史会话后退出")
    parser.add_argument("--debug", action="store_true", help="写入 debug.log 与 events.jsonl")
    args = parser.parse_args()
    cfg = load_config(project_root=args.project_root, overrides={"logging": {"debug": args.debug}})
    workspace, wiki = cfg.paths.resolved_workspace_dir(), cfg.paths.resolved_wiki_dir()
    configure_logging(file_path=str(workspace / "debug.log") if cfg.logging.debug else None)
    registry = ToolRegistry()
    registry.register(ReadFile(wiki, workspace=workspace))
    registry.register(ListDir(wiki))
    registry.register(Grep(wiki))
    agent = ReActAgent(
        name="wiki-qa",
        llm=create_llm(cfg.llm, cfg.retry),
        vlm=create_vlm(cfg.vlm, cfg.retry),
        tool_registry=registry,
        workspace=workspace,
        wiki_dir=wiki,
        agent_config=cfg.agent,
        compile_config=cfg.compile,
        retry_config=cfg.retry,
        hooks=[TerminalRenderer(console)],
    )
    if args.list:
        for key in agent.session_manager.list_session_keys():
            console.print(key)
        return
    asyncio.run(_run(agent, cfg, args.resume or _new_session_key()))
