"""命令路由系统。

参考 nanobot: RESTORE → COMPACT(轻量) → COMMAND → BUILD → RUN → SAVE。
命令分发在 restore 之后、重压缩（maybe_consolidate）之前——
命令需要 session 状态，但不应该触发昂贵的 LLM 压缩。

所有命令统一 `/` 前缀: /help /session /retry
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from wiki_agent.events import CommandProgress, RunContext
from wiki_agent.log import emit_event, get_logger

if TYPE_CHECKING:
    from wiki_agent.agent import ReActAgent
    from wiki_agent.conversation import Session

logger = get_logger("COMMAND")


class CommandReporter:
    """命令进度的统一出口：Hook 即时通知 + 结构化事件。"""

    def __init__(self, hooks, context: RunContext, command: str, task_id: str):
        self._hooks = hooks
        self._context = context
        self.command = command
        self.task_id = task_id

    async def start(self) -> None:
        await self._hooks.on_command_start(self._context, self.command, self.task_id)
        emit_event("command_start", command=self.command, task_id=self.task_id)

    async def progress(
        self,
        stage: str,
        *,
        current: int | None = None,
        total: int | None = None,
        message: str = "",
        level: str = "info",
        data: dict | None = None,
    ) -> None:
        event = CommandProgress(
            task_id=self.task_id,
            command=self.command,
            stage=stage,
            current=current,
            total=total,
            message=message,
            level=level,
            data=data or {},
        )
        await self._hooks.on_command_progress(self._context, event)
        emit_event(
            "command_progress",
            task_id=self.task_id,
            command=self.command,
            stage=stage,
            current=current,
            total=total,
            message=message,
            level=level,
            data=event.data,
        )

    async def end(self, result) -> None:
        await self._hooks.on_command_end(self._context, self.command, self.task_id, result)
        emit_event(
            "command_end",
            command=self.command,
            task_id=self.task_id,
            status=getattr(result, "status", "succeeded"),
        )

    async def error(self, error: BaseException) -> None:
        await self._hooks.on_command_error(self._context, self.command, self.task_id, error)
        emit_event(
            "command_error",
            command=self.command,
            task_id=self.task_id,
            error=type(error).__name__,
            detail=str(error)[:300],
        )

    async def cancelled(self) -> None:
        await self._hooks.on_command_cancelled(self._context, self.command, self.task_id)
        emit_event("command_cancelled", command=self.command, task_id=self.task_id)


@dataclass
class CommandContext:
    """命令分发上下文。"""

    raw: str  # 完整输入，如 "/retry"
    key: str  # 命令名，如 "retry"
    args: str  # 参数部分
    session: Session  # 当前会话（restore 之后）
    agent: ReActAgent
    task_id: str = ""
    reporter: CommandReporter | None = None


@dataclass
class CommandResult:
    """命令执行结果。"""

    text: str | None = None
    """显示给用户的文本（None 表示不显示）。"""

    rerun_with: str | None = None
    """非空则 agent 以该文本作为新 user_input 继续完整流程。

    /retry 用: 不清理历史，追加一条"不满意"指令走正常 build——
    历史原封不动传给模型，模型自然知道如何重构。
    """

    status: str = "succeeded"
    task_id: str | None = None


class Command:
    """单个命令。"""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    async def execute(self, ctx: CommandContext) -> CommandResult | None:
        """执行命令。

        Args:
            ctx: 命令上下文（raw/key/args/session/agent）。

        Returns:
            命令结果；None 表示非命令输入（走正常 LLM 流程）。
        """
        raise NotImplementedError


class CommandRouter:
    """注册 + 匹配 + 分发命令。"""

    def __init__(self):
        self._commands: dict[str, Command] = {}

    def register(self, cmd: Command) -> None:
        """注册命令（同名覆盖）。

        Args:
            cmd: 命令实例。
        """
        self._commands[cmd.name] = cmd

    def all(self) -> list[Command]:
        """返回全部已注册命令。

        Returns:
            命令实例列表。
        """
        return list(self._commands.values())

    def match(self, raw: str) -> tuple[Command, str] | None:
        """匹配输入，返回 (命令, 参数)。

        Args:
            raw: 原始输入（以 / 开头）。

        Returns:
            (命令实例, 参数字符串)；非命令输入返回 None。
        """
        if not raw.startswith("/"):
            return None
        parts = raw[1:].strip().split(maxsplit=1)
        key = parts[0].lower() if parts else ""
        cmd = self._commands.get(key)
        if cmd is None:
            return None
        args = parts[1] if len(parts) > 1 else ""
        return cmd, args

    async def dispatch(
        self,
        raw: str,
        session: Session,
        agent: ReActAgent,
        run_context: RunContext | None = None,
    ) -> CommandResult | None:
        """分发命令。

        CommandContext 在此构造——key/args 由 match 结果填充，
        调用方只传原材料（raw/session/agent），不接触占位值。

        Args:
            raw: 用户原始输入。
            session: 当前会话。
            agent: ReActAgent 实例。

        Returns:
            命令执行结果；非命令输入返回 None（走正常 LLM 流程）。
        """
        matched = self.match(raw)
        if matched is None:
            return None
        cmd, args = matched
        context = run_context or RunContext(session_key=session.key)
        task_id = f"{cmd.name}_{uuid4().hex[:12]}"
        reporter = CommandReporter(agent._hooks, context, cmd.name, task_id)
        ctx = CommandContext(
            raw=raw,
            key=cmd.name,
            args=args,
            session=session,
            agent=agent,
            task_id=task_id,
            reporter=reporter,
        )
        logger.info("执行命令 /%s %s", cmd.name, args)
        await reporter.start()
        try:
            result = await cmd.execute(ctx)
        except asyncio.CancelledError:
            await reporter.cancelled()
            raise
        except Exception as exc:
            await reporter.error(exc)
            raise
        if result is not None:
            result.task_id = task_id
        await reporter.end(result)
        return result


# 内置命令


class HelpCommand(Command):
    name = "help"
    description = "显示所有命令"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        lines = ["# 可用命令", ""]
        for cmd in sorted(ctx.agent.commands.all(), key=lambda c: c.name):
            lines.append(f"- **/{cmd.name}** — {cmd.description}")
        lines.append("")
        lines.append("任何其他输入都会作为问题交给知识库助手。")
        return CommandResult(text="\n".join(lines))


class ResolveCommand(Command):
    """裁决问题库中的用户纠错。"""

    name = "resolve"
    description = "裁决 QA 纠错条目（accept 确认待修 / reject 驳回 / keep 存疑）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from wiki_agent.application.issue_actions import resolve_correction_issue
        from wiki_agent.issues import IssueKind, IssueStatus

        service = ctx.agent.issue_service
        args = ctx.args.strip()
        corrections = service.list(
            statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
            kinds={IssueKind.CONTENT_CORRECTION},
            limit=1000,
        )

        parts = args.split(maxsplit=1)
        action = parts[0].lower() if parts else ""

        if action in ("accept", "reject", "keep"):
            try:
                idx = int(parts[1].strip()) - 1  # 显示序号从 1 开始
            except (IndexError, ValueError):
                return CommandResult(
                    text="# /resolve\n\n用法: `/resolve accept|reject|keep <序号>`"
                )
            if not 0 <= idx < len(corrections):
                return CommandResult(text="# /resolve\n\n序号无效。")
            issue_action = {
                "accept": "accept",
                "reject": "reject",
                "keep": "keep_uncertain",
            }[action]
            try:
                resolve_correction_issue(service, corrections[idx].id, issue_action)
            except (LookupError, RuntimeError, ValueError) as exc:
                return CommandResult(text=f"# /resolve\n\n裁决失败：{exc}")
            verb = {"accept": "✅ 已确认待修", "reject": "🚫 已驳回", "keep": "❓ 标记存疑"}[action]
            return CommandResult(text=f"# /resolve\n\n{verb}: 第 {parts[1]} 条")

        if not corrections:
            return CommandResult(text="# /resolve\n\n没有待裁决的纠错条目。")
        lines = ["# 纠错条目裁决", ""]
        for i, correction in enumerate(corrections, 1):
            lines.append(f"{i}. {correction.summary}")
        lines.append("")
        lines.append(
            "`/resolve accept <n>` 确认待修 · `/resolve reject <n>` 驳回 · `/resolve keep <n>` 存疑"
        )
        return CommandResult(text="\n".join(lines))


class QueueCommand(Command):
    """统一问题中心的 CLI 适配器。"""

    name = "queue"
    description = "查看问题中心（/queue retry <id> / done <id>）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from wiki_agent.issues import IssueStatus

        issue_service = ctx.agent.issue_service
        args = ctx.args.strip()

        if args.startswith("done "):
            item_id = args[5:].strip()
            try:
                issue_service.apply_simple_action(item_id, "dismiss")
            except (LookupError, ValueError):
                return CommandResult(text=f"# 问题中心\n\n未找到或无法忽略: {item_id}")
            return CommandResult(text=f"# 问题中心\n\n✅ 已忽略: {item_id}")

        if args == "retry-all" or args.startswith("retry "):
            from wiki_agent.compiler.workflows.retry import retry_source_failures

            if args == "retry-all":
                issue_id = None
            else:
                issue_id = args[6:].strip()
                if not issue_id:
                    return CommandResult(text="# /queue retry\n\n用法: `/queue retry <issue_id>`")
                try:
                    issue_service.store.require(issue_id)
                except LookupError:
                    return CommandResult(text=f"# source 失败重试\n\n未找到: {issue_id}")
            wiki_dir = RefineCommand._wiki_dir(ctx)
            if wiki_dir is None:
                return CommandResult(
                    text="# source 失败重试\n\n无法确定 wiki 目录（ReadFile 未注册）。"
                )
            try:
                result = await retry_source_failures(
                    issue_service.store,
                    llm=ctx.agent.llm,
                    vlm=ctx.agent.vlm,
                    wiki_dir=wiki_dir,
                    source_records_dir=ctx.agent.workspace / "provenance" / "sources",
                    run_root=ctx.agent.workspace / "runs",
                    compile_config=ctx.agent.compile_config,
                    retry_config=ctx.agent.retry_config,
                    issue_id=issue_id,
                )
            except Exception as exc:
                return CommandResult(
                    text=f"# source 失败重试\n\n执行失败：{type(exc).__name__}: {exc}"
                )

            lines = ["# source 失败重试", ""]
            for item in result["results"]:
                lines.append(f"- `{item['id']}`: {item['status']}")
            if result.get("committed"):
                for outcome in result["results"]:
                    if outcome["status"] == "succeeded":
                        current = issue_service.store.require(outcome["id"])
                        if current.status in {IssueStatus.OPEN, IssueStatus.BLOCKED}:
                            issue_service.store.transition(
                                current.id,
                                IssueStatus.RESOLVED,
                                resolution={"action": "source_retry"},
                                event="source_retry_committed",
                            )
                lines.append("\n✅ 重试成功，Git 已提交。")
            elif result.get("rolled_back"):
                lines.append("\n⚠️ 本次未提交，Wiki 已回撤，问题仍保留。")
            elif result.get("message"):
                lines.append(f"\n{result['message']}")
            return CommandResult(text="\n".join(lines))

        cards = issue_service.list(
            statuses={IssueStatus.OPEN, IssueStatus.BLOCKED, IssueStatus.PROCESSING}
        )
        lines = ["# 问题中心", ""]
        if not cards:
            return CommandResult(text="# 问题中心\n\n✅ 没有待处理问题。")
        lines.append(f"共 **{len(cards)}** 项待处理:\n")
        for card in cards:
            lines.append(
                f"- `{card.id}` [{card.kind}/{card.status}] **{card.title}** — {card.summary[:100]}"
            )
        lines.append("")
        lines.append(
            "处理方式: `/queue retry <issue_id>` 重试资料处理失败项；"
            "`/queue done <issue_id>` 忽略；纠错也可用 `/resolve` 裁决。"
        )
        return CommandResult(text="\n".join(lines))


class ScanCommand(Command):
    """扫描并清理完全重复的 Wiki 页面。"""

    name = "scan"
    description = "扫描 Wiki 质量并自动清理完全重复页面"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from wiki_agent.wiki.quality import (
            cleanup_exact_duplicates,
            format_scan_report,
            scan_wiki,
        )

        wiki = RefineCommand._wiki_dir(ctx)
        if wiki is None:
            return CommandResult(text="# /scan 失败\n\n无法定位 wiki 目录。")

        removed = cleanup_exact_duplicates(wiki)
        issues = scan_wiki(wiki)
        issue_service = getattr(ctx.agent, "issue_service", None)
        if issue_service is not None:
            from wiki_agent.issues.producers import report_quality_findings

            report_quality_findings(
                issue_service,
                issues,
                origin={"mode": "cli", "trigger": "scan_command"},
            )
        report = format_scan_report(issues)
        if removed:
            lines = ["## 自动清理完全重复页面", ""]
            for keep, duplicate in removed:
                lines.append(f"- 保留 `[[{keep}]]`，删除 `[[{duplicate}]]`")
            report += "\n\n" + "\n".join(lines)
        return CommandResult(text=report)


class CompileCommand(Command):
    """调用现有文件夹编译器的 CLI 薄封装。"""

    name = "compile"
    description = "编译指定 source 文件夹并生成 Wiki"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        try:
            args = shlex.split(ctx.args.strip())
        except ValueError as exc:
            return CommandResult(text=f"# /compile 参数错误\n\n{exc}")
        if len(args) > 1:
            return CommandResult(
                text=(
                    "# /compile\n\n用法: `/compile [source_dir]`\n"
                    "省略目录时使用 WIKI_MATERIALS_DIR；路径包含空格时请使用引号。"
                )
            )

        project_root = ctx.agent.workspace.resolve().parent
        try:
            from wiki_agent.application.compile_service import compile_sources

            wiki = RefineCommand._wiki_dir(ctx) or (project_root / "wiki")

            async def report(stage, **kwargs):
                if ctx.reporter is not None:
                    await ctx.reporter.progress(stage, **kwargs)

            run_dir = await compile_sources(
                args[0] if args else None,
                project_root=project_root,
                wiki_dir=wiki,
                progress=report,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return CommandResult(text=(f"# /compile 失败\n\n{type(exc).__name__}: {exc}"))
        return CommandResult(
            text=(
                "# /compile 完成\n\n"
                f"运行目录：`{run_dir}`\n"
                f"diff 报告：`{run_dir / 'compile_diff.md'}`\n"
                f"scan 报告：`{run_dir / 'scan_report.md'}`"
            )
        )


class SessionCommand(Command):
    name = "session"
    description = "查看当前会话统计（消息数 / tokens / 窗口水位）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        s = ctx.session
        cap = ctx.agent.agent_config.context_windows
        win = s.current_window_tokens
        pct = win / cap * 100 if cap else 0

        water = "🟢" if pct < 50 else "🟡" if pct < 80 else "🔴"
        return CommandResult(
            text="\n".join(
                [
                    "# 会话统计",
                    "",
                    f"- 会话: **{s.key}**",
                    f"- 消息数: **{len(s.history)}**",
                    f"- 累计 tokens: **{s.token_cost['total']:,}**",
                    f"- 窗口水位: **{win:,} / {cap:,}** ({pct:.0f}%) {water}",
                ]
            )
        )


class WikiCommand(Command):
    """Wiki Git 历史、差异、回撤和 stale 锁人工处理入口。"""

    name = "wiki"
    description = "Wiki 页面与版本管理：open / search / history / diff / rollback"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from wiki_agent.versioning import WikiGitManager

        wiki = RefineCommand._wiki_dir(ctx)
        if wiki is None:
            return CommandResult(text="# /wiki 失败\n\n无法定位 wiki 目录。")
        args = shlex.split(ctx.args.strip())
        action = args[0].lower() if args else "help"
        try:
            if action == "open":
                if len(args) != 2:
                    return CommandResult(text="# /wiki open\n\n用法: `/wiki open <页面路径>`")
                from wiki_agent.wiki import WikiPageNotFound, read_page

                try:
                    page = read_page(wiki, args[1])
                except WikiPageNotFound:
                    return CommandResult(text=f"找不到 Wiki 页面: `{args[1]}`")
                return CommandResult(text=f"# {page.path}\n\n{page.content}")
            if action == "search":
                if len(args) < 2:
                    return CommandResult(
                        text="# /wiki search\n\n用法: `/wiki search <关键词> [limit]`"
                    )
                limit = 20
                if args[-1].isdigit():
                    limit = int(args[-1])
                    query = " ".join(args[1:-1])
                else:
                    query = " ".join(args[1:])
                if not query:
                    return CommandResult(text="# /wiki search\n\n关键词不能为空。")
                if not 1 <= limit <= 100:
                    return CommandResult(text="`limit` 必须在 1 到 100 之间。")
                from wiki_agent.wiki import search_pages

                pages = search_pages(wiki, query, limit=limit)
                if not pages:
                    return CommandResult(text=f"# Wiki search\n\n没有找到包含 `{query}` 的页面。")
                rows = "\n".join(f"- `{page.path}`" for page in pages)
                return CommandResult(text=f"# Wiki search: {query}\n\n{rows}")
            manager = WikiGitManager(
                wiki, run_root=ctx.agent.workspace / "runs", require_clean=False
            )
            if action == "history":
                limit = int(args[1]) if len(args) > 1 else 20
                rows = manager.history(limit)
                return CommandResult(
                    text="# Wiki history\n\n"
                    + ("\n".join(f"- `{row}`" for row in rows) if rows else "暂无提交记录。")
                )
            if action == "diff":
                if len(args) < 2:
                    return CommandResult(text="# /wiki diff\n\n用法: `/wiki diff <run_id>`")
                diff = manager.diff_for_run(args[1].strip())
                if not diff:
                    diff = "（该运行没有差异，或尚未提交。）"
                return CommandResult(text=f"# Wiki diff: {args[1]}\n\n```diff\n{diff}\n```")
            if action == "rollback":
                if len(args) < 2:
                    return CommandResult(text="# /wiki rollback\n\n用法: `/wiki rollback <run_id>`")
                record = manager.run_record(args[1].strip())
                if not record:
                    return CommandResult(text=f"找不到运行记录: {args[1]}")
                commit = record.get("commit") or record.get("after_commit")
                if not commit or record.get("status") != "committed":
                    return CommandResult(text="只能回撤已提交且有 commit 的运行。")
                new_commit = manager.rollback(commit, run_id=args[1].strip())
                return CommandResult(
                    text=f"# Wiki rollback\n\n已回撤 `{args[1]}`，新回撤提交: `{new_commit}`"
                )
            if action in {"clear-stale", "clear_stale"}:
                status = manager.stale_status()
                lock = status.get("lock")
                if not lock:
                    return CommandResult(text="没有 Wiki Git 锁。")
                if not lock.get("stale"):
                    return CommandResult(text=f"锁仍由 pid={lock.get('pid')} 持有，未清理。")
                manager.clear_stale_lock()
                return CommandResult(text="已清理 stale Wiki Git 锁；未自动回撤文件。")
            if action in {"abort-stale", "abort_stale"}:
                if len(args) < 2:
                    return CommandResult(
                        text="# /wiki abort-stale\n\n"
                        "用法: `/wiki abort-stale <run_id>`\n"
                        "确认回撤: `/wiki abort-stale <run_id> --confirm`"
                    )
                tokens = args[1].split()
                run_id = tokens[0]
                record = manager.run_record(run_id)
                if record is None:
                    return CommandResult(text=f"找不到运行记录: {run_id}")
                if record.get("status") != "active":
                    return CommandResult(text=f"运行不是 active：{record.get('status')}")
                changed = record.get("changed_files", [])
                if "--confirm" not in tokens[1:]:
                    files = (
                        "\n".join(f"- `{path}`" for path in changed) or "- （记录中暂无变更清单）"
                    )
                    return CommandResult(
                        text=(
                            f"# stale run: {run_id}\n\n"
                            f"before_commit: `{record.get('before_commit', '')}`\n\n"
                            f"变更文件:\n{files}\n\n"
                            "确认这可能删除该运行产生的 Wiki 修改后，执行：\n"
                            f"`/wiki abort-stale {run_id} --confirm`"
                        )
                    )
                aborted = manager.abort_stale(run_id)
                return CommandResult(
                    text=(
                        f"# stale run 已回撤\n\n"
                        f"运行 `{run_id}` 已恢复到 `{aborted.before_commit}`，"
                        "状态为 `aborted`。"
                    )
                )
            return CommandResult(
                text=(
                    "# /wiki\n\n"
                    "用法：`/wiki open <页面路径>` · `/wiki search <关键词> [limit]` · "
                    "`/wiki history` · `/wiki diff <run_id>` · "
                    "`/wiki rollback <run_id>` · `/wiki clear-stale` · "
                    "`/wiki abort-stale <run_id> [--confirm]`"
                )
            )
        except Exception as exc:
            return CommandResult(text=f"# /wiki 失败\n\n{type(exc).__name__}: {exc}")


class RetryCommand(Command):
    name = "retry"
    description = "对上一个回答不满意？换个思路重新组织"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        # 找最后一条 user 消息作为原始问题
        original = ""
        for m in reversed(ctx.session.history):
            if m.role == "user":
                original = m.content
                break

        if not original:
            return CommandResult(text="# 没有可重试的内容\n\n还没有进行过任何问答。")

        # 不清理历史——追加一条"不满意"指令走正常 build，
        # 历史原封不动传给模型，模型自然知道如何重构。
        return CommandResult(
            text=f"重新组织思路回答：**{original}**",
            rerun_with=(f"我不喜欢上面的回答。请重新组织思路、换个角度回答我的问题：{original}"),
        )


class RefineCommand(Command):
    name = "refine"
    description = "执行 wiki 精炼；加 --dry-run 只预览结构重组"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """refine 全量 + 结构重组——默认执行，``--dry-run`` 只预览重组。

        Args:
            ctx: 命令上下文。

        Returns:
            执行结果（汇总 refine/重组/扫描统计）。
        """
        from datetime import datetime

        from wiki_agent.compiler.workflows.failures import SourceFailureHandler
        from wiki_agent.compiler.workflows.ingest import CompilePipeline
        from wiki_agent.compiler.workflows.refine import refine_all, refine_pages
        from wiki_agent.versioning import WikiGitManager
        from wiki_agent.wiki.quality import format_scan_report, scan_wiki

        # wiki 目录从 ReadFile 工具拿（root 就是 wiki 根）
        wiki = self._wiki_dir(ctx)
        if wiki is None:
            return CommandResult(text="# /refine 失败\n\n无法定位 wiki 目录。")

        pages = refine_pages(wiki)
        if not pages:
            return CommandResult(text="# /refine\n\n没有可 refine 的页面。")

        dry_run = "--dry-run" in ctx.args.split()
        git_manager = None
        git_run = None
        if not dry_run:
            try:
                git_manager = WikiGitManager(wiki, run_root=ctx.agent.workspace / "runs")
                git_run = git_manager.begin(
                    f"refine_{datetime.now().strftime('%Y%m%d_%H%M%S')}", mode="refine"
                )
            except Exception as exc:
                return CommandResult(text=f"# /refine 未执行\n\nGit 运行前检查失败：{exc}")
        lines = ["# /refine 完成", ""]
        lines.append(f"输入页面: {len(pages)} 个")

        if dry_run:
            lines.append("页面精炼: dry-run（未修改页面）")
        else:
            pipeline = CompilePipeline(
                llm=ctx.agent.llm,
                vlm=ctx.agent.vlm,
                wiki_dir=wiki,
                source_records_dir=ctx.agent.workspace / "provenance" / "sources",
                mode="refine",
                compile_config=ctx.agent.compile_config,
            )
            failure_handler = SourceFailureHandler(ctx.agent.issue_service, mode="refine")
            try:
                stats = await refine_all(
                    pipeline,
                    pages,
                    failure_handler=failure_handler,
                )
            except asyncio.CancelledError as exc:
                if git_manager and git_run:
                    git_manager.abort(git_run, reason=f"refine cancelled: {exc}")
                raise
            except Exception as exc:
                if git_manager and git_run:
                    git_manager.abort(git_run, reason=f"refine exception: {exc}")
                raise
            lines.append(f"成功 {stats['ok']} / 无操作 {stats['noop']} / 失败 {stats['failed']}")

        # 结构重组：复用 application.restructure_service（与独立脚本同一编排），
        # 在 refine 的同一 git 事务内批量执行；命令侧只渲染结果，不重复流程逻辑。
        restructure_result = None
        try:
            from wiki_agent.application.restructure_service import restructure_wiki

            outcome = await restructure_wiki(
                ctx.agent.llm,
                wiki,
                confirm=None,  # 批量全收（无交互）
                dry_run=dry_run,
                issue_service=getattr(ctx.agent, "issue_service", None),
                origin={"mode": "refine", "trigger": "refine_command"},
            )
            lines.append("")
            lines.append(
                f"结构重组: {len(outcome.proposals)} 粗提 → {len(outcome.confirmed)} 确认 → "
                f"{len(outcome.effective)} 有效"
            )
            for p in outcome.effective:
                lines.append(f"- {p.op} {p.pages} → {p.target} | {p.reason[:60]}")
            restructure_result = outcome.result
            if dry_run:
                lines.append("结构重组: dry-run（未修改结构页面）")
            elif outcome.result is not None:
                lines.append(
                    f"结构重组已执行: {len(outcome.result.actions)} 成功 / "
                    f"{len(outcome.result.skipped)} 跳过"
                )
            if outcome.unresolved:
                lines.append(f"结构重组: {len(outcome.unresolved)} 组冲突已记入问题中心")
        except asyncio.CancelledError as exc:
            if git_manager and git_run:
                git_manager.abort(git_run, reason=f"restructure cancelled: {exc}")
            raise
        except Exception as e:
            lines.append(f"结构重组跳过: {type(e).__name__}: {str(e)[:100]}")

        issues = scan_wiki(wiki)
        issue_service = getattr(ctx.agent, "issue_service", None)
        if issue_service is not None:
            from wiki_agent.issues.producers import report_quality_findings

            report_quality_findings(
                issue_service,
                issues,
                origin={"mode": "refine", "trigger": "refine_command"},
            )
        lines.append("")
        lines.append(format_scan_report(issues))

        if git_manager and git_run:
            scan_report = git_run.run_dir / "scan_report.md"
            scan_report.write_text(format_scan_report(issues), encoding="utf-8")
            errors = [issue for issue in issues if issue.level == "error"]
            skipped = len(restructure_result.skipped) if restructure_result else 0
            if errors or skipped:
                git_manager.abort(
                    git_run,
                    reason=f"refine validation failed: errors={len(errors)}, skipped={skipped}",
                )
                lines.append("Git: 已恢复到运行前版本")
            else:
                committed = git_manager.commit(
                    git_run,
                    message=f"wiki: refine {git_run.run_id}",
                    scan_report=scan_report,
                    metadata={"scan_errors": len(errors), "restructure_skipped": skipped},
                )
                lines.append(f"Git: 已提交 {committed.commit}")

        return CommandResult(text="\n".join(lines))

    @staticmethod
    def _wiki_dir(ctx: CommandContext) -> Path | None:
        """从工具注册表拿 wiki 根（ReadFile._root）。

        Args:
            ctx: 命令上下文。

        Returns:
            wiki 根路径；ReadFile 未注册时返回 None。
        """
        registry = getattr(ctx.agent, "tool_registry", None)
        if registry is None:
            return None
        read_file = registry.get("ReadFile")
        if read_file is None:
            return None
        return Path(read_file._root)


# 内置命令聚合


def create_command_router() -> CommandRouter:
    """创建已注册全部内置命令的 router。

    Returns:
        包含全部内置命令的 CommandRouter。
    """
    router = CommandRouter()
    for cmd in (
        HelpCommand(),
        SessionCommand(),
        RetryCommand(),
        WikiCommand(),
        RefineCommand(),
        CompileCommand(),
        ScanCommand(),
        QueueCommand(),
        ResolveCommand(),
    ):
        router.register(cmd)
    return router
