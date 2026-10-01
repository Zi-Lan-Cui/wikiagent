"""应用驱动的斜杠命令——编排用例，故住 application，不反向压在 agent 上。

这三条命令调用应用用例（run_maintain/gate_text/resolve_correction_issue）
并格式化其结果类型（MaintainFlow/MaintenanceOutcome）。把它们留在
agent.commands 会让 agent 包反向 import application，构成包级环。命令框
架（Command/CommandContext/CommandResult）与只依赖 agent+infra 的原生命令
仍在 agent.commands；组合根把本模块的命令注册进同一个 router 注入 agent。
"""

from __future__ import annotations

from wiki_agent.agent.commands import Command, CommandContext, CommandResult, wiki_root
from wiki_agent.application.issue_actions import resolve_correction_issue
from wiki_agent.application.maintenance_flow import gate_text, run_maintain
from wiki_agent.issues import IssueKind, IssueStatus
from wiki_agent.jobs import Kind, PipelineBusy, SyncBaselineLag


class MaintainCommand(Command):
    """结构维护：全库分析 → 单元提议 → 确认后整批入队（批尾自动补链）。"""

    name = "maintain"
    description = "结构重组入队（单元 + 批尾补链）；--dry-run 只预览提议"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """/maintain 不直接写 wiki——流程（预检→提议→入队）经 run_maintain，
        命令只解析参数与格式化。逐条确认走 scripts/restructure_wiki.py，
        本命令无交互全收；整批撤销用 ``/wiki revert-batch <batch_id>``。
        """
        wiki = wiki_root(ctx)
        if wiki is None:
            return CommandResult(text="# /maintain 失败\n\n无法定位 wiki 目录。")
        job_service = ctx.agent.job_service
        if job_service is None:
            return CommandResult(text="# /maintain\n\n当前会话未装配任务队列（无执行入口）。")
        tokens = ctx.args.split()
        bad = [t for t in tokens if t != "--dry-run"]
        if bad:
            return CommandResult(
                text=f"# /maintain 参数不识别: {bad}\n\n用法: `/maintain [--dry-run]`"
            )
        dry_run = "--dry-run" in tokens
        flow = await run_maintain(ctx.agent.llm, wiki, job_service, dry_run=dry_run)
        if flow.blocked:
            return CommandResult(text=f"# /maintain 提交暂拒\n\n{flow.blocked}")
        if flow.error:
            return CommandResult(text=f"# /maintain 分析失败\n\n{flow.error}")
        outcome = flow.outcome
        assert outcome is not None
        lines = ["# /maintain dry-run" if dry_run else "# /maintain", ""]
        lines.append(
            f"提议: {len(outcome.proposed)} 初提 → {len(outcome.confirmed)} 复核保留 → "
            f"{len(outcome.effective)} 可执行；放弃 {len(outcome.rejected)}（复核）"
            f"+ {len(outcome.dropped)}（消解）"
        )
        for unit, reason in outcome.rejected + outcome.dropped:
            lines.append(f"- 放弃 {'+'.join(unit.in_pages)} — {reason[:80]}")
        if outcome.healthy:
            lines.append("结构健康，无需动手。")
            return CommandResult(text="\n".join(lines))
        if not outcome.effective:
            lines.append("有建议但全部被消解拒绝（理由见上）——未入队。")
            return CommandResult(text="\n".join(lines))
        for unit in outcome.effective:
            out = "+".join(unit.out_slugs) or "（删除）"
            lines.append(f"- {'+'.join(unit.in_pages)} → {out} | {unit.reason[:60]}")
        if dry_run:
            lines.append("\ndry-run：未入队。")
            return CommandResult(text="\n".join(lines))
        if flow.submit_rejected:
            return CommandResult(text="\n".join(lines) + f"\n\n{flow.submit_rejected}")
        if not flow.jobs:
            return CommandResult(text="\n".join(lines) + "\n\n没有可入队的单元。")
        batch = str(flow.jobs[0].payload.get("batch") or "")
        n_units = sum(1 for j in flow.jobs if j.kind == Kind.RESTRUCTURE)
        lines.append(
            f"\n已入队: {n_units} 个单元 + {len(flow.jobs) - n_units} 个补链"
            f"（批 {batch}；某单元核对不过只撤该单元；整批回撤: /wiki revert-batch {batch}）"
        )
        return CommandResult(text="\n".join(lines))


class LinkCommand(Command):
    """关联扫：给指定页（默认全库内容页）补充/修正 wikilink。"""

    name = "link"
    description = "全库（或指定页）出链维护入队；发现型补链的手动入口"

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """/link [页...] 入队一批 link job（一页一 job、一页一提交）。

        维护批的批尾 link 只覆盖波及面；"老页该链新页"这类发现型需求由
        这里的全库扫承接。互斥与基线检查与 /maintain 同一套，前置执行。
        """
        wiki = wiki_root(ctx)
        if wiki is None:
            return CommandResult(text="# /link 失败\n\n无法定位 wiki 目录。")
        job_service = ctx.agent.job_service
        if job_service is None:
            return CommandResult(text="# /link\n\n当前会话未装配任务队列（无执行入口）。")
        tokens = ctx.args.split()
        flags = [t for t in tokens if t.startswith("-")]
        if flags:
            return CommandResult(
                text=f"# /link 参数不识别: {flags}\n\n用法: `/link [页slug...]`"
            )
        blocked = gate_text(job_service)
        if blocked:
            return CommandResult(text=f"# /link 提交暂拒\n\n{blocked}")
        try:
            jobs = job_service.submit_link_batch(slugs=tokens or None)
        except (PipelineBusy, SyncBaselineLag, ValueError) as exc:
            return CommandResult(text=f"# /link 提交暂拒\n\n{exc}")
        if not jobs:
            return CommandResult(text="# /link\n\n没有可扫描的页面。")
        scope = "、".join(tokens) if tokens else "全库内容页"
        return CommandResult(
            text=f"# /link 已入队\n\n范围: {scope}——{len(jobs)} 个 link job（一页一提交）。"
        )


class ResolveCommand(Command):
    """裁决问题库中的用户纠错。"""

    name = "resolve"
    description = "裁决 QA 纠错条目（accept 确认待修 / reject 驳回 / keep 存疑）"

    async def execute(self, ctx: CommandContext) -> CommandResult:
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


def create_application_commands() -> list[Command]:
    """应用驱动命令实例——由组合根注册进 agent 原生命令的同一 router。"""
    return [MaintainCommand(), LinkCommand(), ResolveCommand()]
