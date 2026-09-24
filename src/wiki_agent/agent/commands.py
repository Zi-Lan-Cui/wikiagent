"""命令路由系统。

回合流程: RESTORE → COMPACT(轻量) → COMMAND → BUILD → RUN → SAVE——
命令分发在 restore 之后、重压缩之前：命令需要 session 状态，
但不应触发昂贵的 LLM 压缩。

所有命令统一 `/` 前缀: /help /session /retry
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from wiki_agent.config import load_config
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
        reporter = CommandReporter(agent.hooks, context, cmd.name, task_id)
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


def _in_flight_wiki_jobs(agent: ReActAgent) -> int:
    """写 wiki 的 job（compile/delete/refine/restructure）在途数。

    跨进程互斥由执行锁（flock）强制，这条门只管同进程：/wiki revert
    入口带 restore——同进程正在执行写 wiki 的任务时拒绝改历史，
    否则会把正在执行的未提交改动清掉。
    """
    # 能力是显式声明的可选属性（ReActAgent.job_service），缺席=本会话无执行入口
    if agent.job_service is None:
        return 0
    return agent.job_service.wiki_write_in_flight()


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
            from wiki_agent.issues import IssueKind
            from wiki_agent.jobs.retry_source import SourceUnavailableError
            from wiki_agent.jobs.service import JobService

            job_service: JobService | None = ctx.agent.job_service
            if job_service is None:
                return CommandResult(
                    text="# source 失败重试\n\n当前进程未接入 Job 队列（仅组装了 job_service 的入口可用）。"
                )
            if args == "retry-all":
                ids = [
                    card.id
                    for card in issue_service.list(
                        statuses={IssueStatus.OPEN}, kinds={IssueKind.INGESTION_FAILURE}
                    )
                ]
                if not ids:
                    return CommandResult(text="# source 失败重试\n\n没有待重试的资料失败项。")
            else:
                one = args[6:].strip()
                if not one:
                    return CommandResult(text="# /queue retry\n\n用法: `/queue retry <issue_id>`")
                ids = [one]

            lines = ["# source 失败重试（移交 Job 队列）", ""]
            for issue_id in ids:
                try:
                    job = job_service.submit_issue_retry(issue_id)
                except LookupError:
                    lines.append(f"- `{issue_id}`: 未找到")
                except SourceUnavailableError as exc:
                    lines.append(f"- `{issue_id}`: 输入不可用 — {exc}")
                except ValueError as exc:
                    lines.append(f"- `{issue_id}`: 不能重试 — {exc}")
                else:
                    lines.append(f"- `{issue_id}`: 已排队 `{job.id}`，由 Worker 串行执行")
            lines.append("")
            lines.append(
                "执行结果稍后用 `/queue` 查看（成功自动销账，失败记回问题中心等人工处理）。"
            )
            return CommandResult(text="\n".join(lines))

        cards = issue_service.list(statuses={IssueStatus.OPEN, IssueStatus.BLOCKED})
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
        from wiki_agent.issues.producers import report_quality_findings

        report_quality_findings(
            ctx.agent.issue_service,
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
    """/compile = 拍一次快照 sync——写 wiki 只有队列一条路。

    没有独立的批编译通道：空账本时快照差集=全部文件，首跑天然全量；
    有账本时就是增量。任务入队后由本进程的 worker 泵执行，逐文件提交。
    """

    name = "compile"
    description = "编译 source 文件夹（一次快照 sync；首次运行即全量编译）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from wiki_agent.jobs import SyncInProgress

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
        job_service = ctx.agent.job_service
        if job_service is None:
            return CommandResult(text="# /compile\n\n当前会话未装配任务队列（无执行入口）。")

        target = (
            Path(args[0]).expanduser().resolve()
            if args
            else load_config(project_root=ctx.agent.workspace.resolve().parent).paths.resolved_materials_dir()
        )
        if not target.is_dir():
            return CommandResult(text=f"# /compile\n\n源目录不存在: {target}")
        try:
            jobs = job_service.submit_sync(target)
        except SyncInProgress:
            return CommandResult(text="# /compile\n\n上一批快照仍在执行（互斥串行）——等它跑完再拍。")
        return CommandResult(
            text=(
                "# /compile 已入队\n\n"
                f"源目录：`{target}`\n"
                f"快照任务：{len(jobs)} 个（首跑空账本 = 全量编译）\n"
                "后台逐文件执行并各自提交；进度看工作台，失败保持待同步、再点即重试。"
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
    """Wiki Git 历史查询与版本回撤入口。

    版本身份 = commit（HEAD 即最近已结算状态，不再有 run 容器概念）；
    撤销一批 sync = 按 commit 尾注 `Batch: <id>` 选段 revert。
    """

    name = "wiki"
    description = "Wiki 页面与版本管理：open / search / history / diff / revert / revert-batch"

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
            manager = WikiGitManager(wiki)
            if action == "history":
                limit = int(args[1]) if len(args) > 1 else 20
                rows = manager.history(limit)
                return CommandResult(
                    text="# Wiki history\n\n"
                    + ("\n".join(f"- `{row}`" for row in rows) if rows else "暂无提交记录。")
                )
            if action == "diff":
                if len(args) < 2:
                    return CommandResult(text="# /wiki diff\n\n用法: `/wiki diff <commit>`")
                diff = manager.diff_commit(args[1].strip())
                if not diff:
                    diff = "（该版本没有 Wiki 差异。）"
                return CommandResult(text=f"# Wiki diff: {args[1]}\n\n```diff\n{diff}\n```")
            if action in {"revert", "revert-batch", "revert_batch"}:
                # 回撤入口自带 restore——有活在跑就不碰历史（与 sync 互斥闸同一语义）
                if _in_flight_wiki_jobs(ctx.agent) > 0:
                    return CommandResult(
                        text="存在在途写 wiki 任务，拒绝版本回撤——先等队列跑完。"
                    )
            if action == "revert":
                if len(args) < 2:
                    return CommandResult(text="# /wiki revert\n\n用法: `/wiki revert <commit>`")
                new_commit = manager.revert_commit(args[1].strip())
                return CommandResult(
                    text=f"# Wiki revert\n\n已回撤 `{args[1]}`，反向提交: `{new_commit[:8]}`"
                )
            if action in {"revert-batch", "revert_batch"}:
                if len(args) < 2:
                    return CommandResult(
                        text="# /wiki revert-batch\n\n用法: `/wiki revert-batch <batch_id>`\n\n"
                        "batch_id 来自 sync 快照批 commit 的 `Batch:` 尾注（`/wiki history` 或任务详情）。\n"
                        "回撤是纯历史操作：sync 完成账不随之回退。"
                    )
                new_commit = manager.revert_batch(args[1].strip())
                return CommandResult(
                    text=f"# 批回撤\n\n批次 `{args[1]}` 已回撤，反向提交: `{new_commit[:8]}`"
                )
            return CommandResult(
                text=(
                    "# /wiki\n\n"
                    "用法：`/wiki open <页面路径>` · `/wiki search <关键词> [limit]` · "
                    "`/wiki history [limit]` · `/wiki diff <commit>` · "
                    "`/wiki revert <commit>` · `/wiki revert-batch <batch_id>`"
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
    description = "把 wiki 精炼与结构重组排进队列；--dry-run 只预览重组提议"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """/refine 不直接写 wiki，与 sync 同一条队列。

        - 非 dry-run：refine 逐页入队（一页一 job、一页一提交）；重组在
          提交侧同步跑提议阶段（粗提→复判→消解），有效提议切成执行单元
          入队、一单元一 job 一提交——无交互全收，逐条确认在脚本侧；
        - dry-run：什么都不入队，只预览重组提议。
        执行由后台泵串行完成：refine 每页失败只撤该页，重组 job 自带
        scan 闸门（error/skipped 整批撤销）；想撤销整批用
        ``/wiki revert-batch <batch_id>``。
        """
        from dataclasses import asdict

        from wiki_agent.application.restructure_service import restructure_wiki
        from wiki_agent.compiler.workflows.refine import refine_pages

        wiki = self._wiki_dir(ctx)
        if wiki is None:
            return CommandResult(text="# /refine 失败\n\n无法定位 wiki 目录。")
        pages = refine_pages(wiki)
        if not pages:
            return CommandResult(text="# /refine\n\n没有可 refine 的页面。")
        job_service = ctx.agent.job_service
        if job_service is None:
            return CommandResult(text="# /refine\n\n当前会话未装配任务队列（无执行入口）。")
        dry_run = "--dry-run" in ctx.args.split()

        lines = ["# /refine 已入队" if not dry_run else "# /refine dry-run", ""]
        lines.append(f"输入页面: {len(pages)} 个")
        if dry_run:
            lines.append("页面精炼: dry-run（未入队、未修改）")
        else:
            jobs = job_service.submit_refine_batch()
            lines.append(f"页面精炼: {len(jobs)} 个 refine job 已入队（一页一提交）")

        try:
            outcome = await restructure_wiki(ctx.agent.llm, wiki, confirm=None, dry_run=True)
        except Exception as e:
            lines.append(f"结构重组跳过: {type(e).__name__}: {str(e)[:100]}")
            return CommandResult(text="\n".join(lines))
        lines.append("")
        lines.append(
            f"结构重组: {len(outcome.proposals)} 粗提 → {len(outcome.confirmed)} 复判确认 → "
            f"{len(outcome.effective)} 有效"
        )
        for p in outcome.effective:
            lines.append(f"- {p.op} {p.pages} → {p.target} | {p.reason[:60]}")
        if outcome.unresolved:
            lines.append(f"{len(outcome.unresolved)} 组冲突放弃执行（结构决定权在你）")

        if dry_run:
            lines.append("结构重组: dry-run（未入队）")
        elif not outcome.effective:
            lines.append("结构重组: 无有效提议，未入队")
        else:
            jobs = job_service.submit_restructure([asdict(p) for p in outcome.effective])
            lines.append(
                f"结构重组: {len(outcome.effective)} 条提议切成 {len(jobs)} 个执行单元入队"
                f"（批 {jobs[0].payload['batch']}，某单元校验不过只撤销该单元；"
                f"整批回撤: /wiki revert-batch {jobs[0].payload['batch']}）"
            )
        if not dry_run:
            lines.extend(["", "队列串行执行（sync 在途者先行）；进度与结果看工作台。"])
        return CommandResult(text="\n".join(lines))

    @staticmethod
    def _wiki_dir(ctx: CommandContext) -> Path | None:
        """从工具注册表拿 wiki 根（ReadFile 的公开 root 属性）。

        Args:
            ctx: 命令上下文。

        Returns:
            wiki 根路径；ReadFile 未注册时返回 None。
        """
        read_file = ctx.agent.tool_registry.get("ReadFile")
        if read_file is None:
            return None
        return Path(read_file.root)


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
