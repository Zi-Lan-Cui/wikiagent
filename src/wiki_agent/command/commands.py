"""命令路由系统。

参考 nanobot: RESTORE → COMPACT(轻量) → COMMAND → BUILD → RUN → SAVE。
命令分发在 restore 之后、重压缩（maybe_consolidate）之前——
命令需要 session 状态，但不应该触发昂贵的 LLM 压缩。

所有命令统一 `/` 前缀: /help /session /retry
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from wiki_agent.log import get_logger

if TYPE_CHECKING:
    from wiki_agent.agent import ReActAgent
    from wiki_agent.session import Session

logger = get_logger("COMMAND")


@dataclass
class CommandContext:
    """命令分发上下文。"""

    raw: str          # 完整输入，如 "/retry"
    key: str          # 命令名，如 "retry"
    args: str         # 参数部分
    session: Session  # 当前会话（restore 之后）
    agent: "ReActAgent"


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


class Command:
    """单个命令。"""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    async def execute(self, ctx: CommandContext) -> CommandResult | None:
        """执行命令。返回 None 表示非命令输入（走正常 LLM 流程）。"""
        raise NotImplementedError


class CommandRouter:
    """注册 + 匹配 + 分发命令。"""

    def __init__(self):
        self._commands: dict[str, Command] = {}

    def register(self, cmd: Command) -> None:
        self._commands[cmd.name] = cmd

    def all(self) -> list[Command]:
        return list(self._commands.values())

    def match(self, raw: str) -> tuple[Command, str] | None:
        """匹配输入，返回 (命令, 参数)。非命令输入返回 None。"""
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
        self, raw: str, session: "Session", agent: "ReActAgent",
    ) -> CommandResult | None:
        """分发命令。非命令输入返回 None（走正常 LLM 流程）。

        CommandContext 在此构造——key/args 由 match 结果填充，
        调用方只传原材料（raw/session/agent），不接触占位值。
        """
        matched = self.match(raw)
        if matched is None:
            return None
        cmd, args = matched
        ctx = CommandContext(
            raw=raw, key=cmd.name, args=args, session=session, agent=agent)
        logger.info("执行命令 /%s %s", cmd.name, args)
        return await cmd.execute(ctx)


# ════════════════════════════════════════════════════════════
#  内置命令
# ════════════════════════════════════════════════════════════

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
    """QA 矛盾裁决——corrections.md 的逐条处置。

    /resolve                列出纠错条目（带序号）
    /resolve accept <n>     确认待修——用户对，纠错保留待修清单
    /resolve reject <n>     驳回——wiki 对，用户观点从清单移除
    /resolve keep <n>       存疑——保留但不处理，标记 [存疑]

    与 /queue 的分工: /queue 看全局待处理（含纠错聚合），
    /resolve 专门裁决纠错条目。事件流（history.jsonl/events）
    仍是事实源——裁决只改清单状态，不抹历史。
    """

    name = "resolve"
    description = "裁决 QA 纠错条目（accept 确认待修 / reject 驳回 / keep 存疑）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        store = ctx.agent.memory_store
        args = ctx.args.strip()
        corrections = store.get_corrections()

        parts = args.split(maxsplit=1)
        action = parts[0].lower() if parts else ""

        if action in ("accept", "reject", "keep"):
            try:
                idx = int(parts[1].strip()) - 1  # 显示序号从 1 开始
            except (IndexError, ValueError):
                return CommandResult(text="# /resolve\n\n用法: "
                                     "`/resolve accept|reject|keep <序号>`")
            if action == "reject":
                ok = store.remove_correction(idx)
            elif action == "accept":
                ok = store.mark_correction(idx, "[已确认待修]")
            else:
                ok = store.mark_correction(idx, "[存疑]")
            if not ok:
                return CommandResult(text="# /resolve\n\n序号无效。")
            verb = {"accept": "✅ 已确认待修", "reject": "🚫 已驳回",
                    "keep": "❓ 标记存疑"}[action]
            return CommandResult(text=f"# /resolve\n\n{verb}: 第 {parts[1]} 条")

        if not corrections:
            return CommandResult(text="# /resolve\n\n没有待裁决的纠错条目。")
        lines = ["# 纠错条目裁决", ""]
        for i, corr in enumerate(corrections, 1):
            lines.append(f"{i}. {corr}")
        lines.append("")
        lines.append("`/resolve accept <n>` 确认待修 · "
                     "`/resolve reject <n>` 驳回 · `/resolve keep <n>` 存疑")
        return CommandResult(text="\n".join(lines))


class QueueCommand(Command):
    """统一异常队列——待处理事项的人机接口。

    /queue            列出全部待处理项（ingest 失败/手术冲突/纠错）
    /queue done <id>  处理完成，移除该项
    纠错项的处理走 /resolve（接受/驳回/留观），/queue 只聚合展示。
    """

    name = "queue"
    description = "查看待处理异常队列（/queue done <id> 移除已处理项）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from wiki_agent.queue import QueueStore

        store = QueueStore(ctx.agent.workspace)
        args = ctx.args.strip()

        if args.startswith("done "):
            item_id = args[5:].strip()
            if not store.remove(item_id):
                return CommandResult(text=f"# 队列\n\n未找到: {item_id}")
            return CommandResult(text=f"# 队列\n\n✅ 已移除: {item_id}")

        items = store.list()
        # corrections 聚合视图——corrections.md 也是待处理事项
        corrections = ctx.agent.memory_store.get_corrections()

        lines = ["# 待处理队列", ""]
        if not items and not corrections:
            lines.append("✅ 队列为空——没有待处理事项。")
            return CommandResult(text="\n".join(lines))

        type_names = {
            "ingest_failure": "ingest 失败",
            "surgery_conflict": "手术冲突",
            "correction": "wiki 纠错",
        }
        lines.append(f"共 **{len(items) + len(corrections)}** 项待处理:\n")

        for item in items:
            t = type_names.get(item.get("type"), item.get("type"))
            extra = item.get("file") or item.get("detail") or ""
            lines.append(
                f"- `{item['id']}` [{t}] {extra} "
                f"({str(item.get('error') or item.get('stage') or '')[:80]})")
        for corr in corrections:
            lines.append(f"- [correction] {corr}")

        lines.append("")
        lines.append("处理方式: `/queue done <id>` 移除已处理项；"
                     "纠错项用 `/resolve` 裁决。")
        return CommandResult(text="\n".join(lines))


class SessionCommand(Command):
    name = "session"
    description = "查看当前会话统计（消息数 / tokens / 窗口水位）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        s = ctx.session
        cap = ctx.agent.agent_config.context_windows
        win = s.current_window_tokens
        pct = win / cap * 100 if cap else 0

        water = "🟢" if pct < 50 else "🟡" if pct < 80 else "🔴"
        return CommandResult(text="\n".join([
            "# 会话统计",
            "",
            f"- 会话: **{s.key}**",
            f"- 消息数: **{len(s.history)}**",
            f"- 累计 tokens: **{s.token_cost['total']:,}**",
            f"- 窗口水位: **{win:,} / {cap:,}** ({pct:.0f}%) {water}",
        ]))


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
            rerun_with=(
                f"我不喜欢上面的回答。请重新组织思路、换个角度回答我的问题："
                f"{original}"
            ),
        )


class RefineCommand(Command):
    name = "refine"
    description = "触发 wiki 精炼链（自编译 + 结构手术 dry-run）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """refine 全量 + 结构手术 dry-run——复用 agent 的 llm/vlm。"""
        from pathlib import Path

        from wiki_agent.compiler.pipeline import CompilePipeline
        from wiki_agent.compiler.quality import scan_wiki
        from wiki_agent.compiler.refine import refine_all, refine_pages

        # wiki 目录从 ReadFile 工具拿（root 就是 wiki 根）
        wiki = self._wiki_dir(ctx)
        if wiki is None:
            return CommandResult(text="# /refine 失败\n\n无法定位 wiki 目录。")

        pages = refine_pages(wiki)
        if not pages:
            return CommandResult(text="# /refine\n\n没有可 refine 的页面。")

        lines = ["# /refine 完成", ""]
        lines.append(f"输入页面: {len(pages)} 个")

        pipeline = CompilePipeline(
            llm=ctx.agent.llm, vlm=ctx.agent.vlm, wiki_dir=wiki, mode="refine")
        stats = await refine_all(pipeline, pages)
        lines.append(f"成功 {stats['ok']} / 无操作 {stats['noop']} / 失败 {stats['failed']}")

        # 结构手术 dry-run（只出报告不动手）
        try:
            from wiki_agent.compiler.surgery import (
                _load_pages, propose_from_index, re_arbitrate, recheck, resolve_conflicts)
            proposals = await propose_from_index(ctx.agent.llm, wiki)
            confirmed, _ = await recheck(ctx.agent.llm, wiki, proposals)
            clean, conflicts = resolve_conflicts(confirmed, _load_pages(wiki))
            if conflicts:
                arb = await re_arbitrate(ctx.agent.llm, wiki, conflicts)
                clean.extend(arb.resolved)
            lines.append("")
            lines.append(
                f"结构手术: {len(proposals)} 粗提 → {len(confirmed)} 确认 → {len(clean)} 有效")
            for p in clean:
                lines.append(f"- {p.op} {p.pages} → {p.target} | {p.reason[:60]}")
        except Exception as e:
            lines.append(f"结构手术跳过: {type(e).__name__}: {str(e)[:100]}")

        issues = scan_wiki(wiki)
        errors = [i for i in issues if i.level == "error"]
        warns = [i for i in issues if i.level == "warning"]
        lines.append(f"扫描: {len(errors)} 错误 / {len(warns)} 警告")

        return CommandResult(text="\n".join(lines))

    @staticmethod
    def _wiki_dir(ctx: CommandContext) -> Path | None:
        """从工具注册表拿 wiki 根（ReadFile._root）。"""
        registry = getattr(ctx.agent, "tool_registery", None)
        if registry is None:
            return None
        read_file = registry.get("ReadFile")
        if read_file is None:
            return None
        return Path(read_file._root)


# ════════════════════════════════════════════════════════════
#  内置命令聚合
# ════════════════════════════════════════════════════════════

def create_command_router() -> CommandRouter:
    """创建已注册全部内置命令的 router。"""
    router = CommandRouter()
    for cmd in (HelpCommand(), SessionCommand(), RetryCommand(),
                RefineCommand(), QueueCommand(), ResolveCommand()):
        router.register(cmd)
    return router
