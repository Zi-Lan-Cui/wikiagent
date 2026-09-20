from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from wiki_agent.application import AppRuntime, WikiAgentService
from wiki_agent.errors import RetryableError
from wiki_agent.log.logger import configure_logging
from wiki_agent.render import TerminalRenderer

console = Console()


def _footer(elapsed: float, total_tokens: int, win: int, cap: int) -> Panel:
    text = Text(f"⏱ {elapsed:.1f}s  │  🪙 {total_tokens:,}", style="dim")
    if cap:
        text.append(f"  │  📊 {win:,} / {cap:,}")
    return Panel(text, border_style="cyan")


async def _interactive_loop(service: WikiAgentService, session_key: str) -> None:
    agent = service.runtime.agent
    console.print(Panel(f"模型: {agent.llm.model_id}\n会话: {session_key}", title="Wiki Agent"))
    dream_task = asyncio.create_task(agent.dream_loop(interval=agent.agent_config.dream_interval))
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
                await service.send_message(session_id=session_key, text=user_input)
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


async def _run(service: WikiAgentService, session_key: str) -> None:
    runtime = service.runtime
    async with runtime:
        await _interactive_loop(service, session_key)


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
    runtime = AppRuntime.from_project_root(
        args.project_root,
        debug=args.debug,
        hooks=[TerminalRenderer(console)],
    )
    configure_logging(
        file_path=str(runtime.workspace / "debug.log") if runtime.config.logging.debug else None
    )
    service = WikiAgentService(runtime)
    if args.list:
        for session in service.list_sessions():
            console.print(f"{session.id}\t{session.title}")
        return
    if args.resume:
        session_id = args.resume
        service.get_session(session_id)
    else:
        session_id = service.create_session().id
    asyncio.run(_run(service, session_id))
