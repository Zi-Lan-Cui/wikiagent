"""结构重组编排 service——脚本与交互命令共用的核心流程。

只负责 propose→recheck→resolve→re_arbitrate→(确认)→execute 这一段；
git 事务、run 目录、面向用户的呈现由各调用方负责（脚本与命令的
git/交互语义不同）。

`confirm` 回调把"哪些有效提议真正执行"交给调用方决定；返回结构化
结果供调用方渲染与 git 决策。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wiki_agent.compiler.restructure import (
    Conflict,
    Proposal,
    SurgeryResult,
    _load_pages,
    execute,
    propose_from_index,
    re_arbitrate,
    recheck,
    resolve_conflicts,
)
from wiki_agent.log import get_logger

logger = get_logger("RESTRUCTURE")

ConfirmCallback = Callable[[list[Proposal]], Awaitable[list[Proposal]]]


@dataclass
class RestructureOutcome:
    proposals: list[Proposal] = field(default_factory=list)
    confirmed: list[Proposal] = field(default_factory=list)
    rejected: list[tuple[Proposal, str]] = field(default_factory=list)
    effective: list[Proposal] = field(default_factory=list)  # 消解+复裁后的有效提议
    accepted: list[Proposal] = field(default_factory=list)  # confirm 回调过滤后要执行的
    unresolved: list[Conflict] = field(default_factory=list)
    result: SurgeryResult | None = None
    healthy: bool = False  # 无提议或全部被否 → 结构健康、无需动手


async def restructure_wiki(
    llm: Any,
    wiki_dir: Path,
    *,
    confirm: ConfirmCallback | None = None,
    dry_run: bool = False,
    issue_service: Any | None = None,
    origin: dict[str, Any] | None = None,
) -> RestructureOutcome:
    """跑一遍结构重组核心流程。返回结构化结果；调用方据此渲染与决定 git 提交/回滚。"""
    wiki_dir = Path(wiki_dir)
    out = RestructureOutcome()

    out.proposals = await propose_from_index(llm, wiki_dir)
    logger.info("粗提: %d 条原子提议", len(out.proposals))
    if not out.proposals:
        out.healthy = True
        logger.info("无提议——结构健康。")
        return out

    out.confirmed, out.rejected = await recheck(llm, wiki_dir, out.proposals)
    for prop, reason in out.rejected:
        logger.info("复判否决 [%s] %s — %s", prop.op, prop.pages, reason[:100])
    if not out.confirmed:
        out.healthy = True
        logger.info("复判全部否决——结构健康。")
        return out

    clean, conflicts = resolve_conflicts(out.confirmed, _load_pages(wiki_dir))
    if conflicts:
        logger.info("确定性消解后仍冲突 %d 组 → LLM 复裁", len(conflicts))
        arb = await re_arbitrate(llm, wiki_dir, conflicts)
        clean.extend(arb.resolved)
        out.unresolved = arb.unresolved
        if out.unresolved and issue_service is not None:
            from wiki_agent.issues.producers import report_restructure_conflicts

            report_restructure_conflicts(
                issue_service, out.unresolved, origin={**(origin or {}), "stage": "arbitration"}
            )
            logger.warning("%d 组冲突无法仲裁——已记入问题中心", len(out.unresolved))

    out.effective = clean
    if not out.effective:
        out.healthy = True
        logger.info("消解后无有效提议。")
        return out

    out.accepted = await confirm(out.effective) if confirm is not None else list(out.effective)
    if dry_run or not out.accepted:
        logger.info(
            "dry-run/无确认——未执行（有效 %d、确认 %d）", len(out.effective), len(out.accepted)
        )
        return out

    out.result = execute(wiki_dir, out.accepted)
    logger.info(
        "执行完成: %d 动作 / 跳过 %d / 备份 %d 文件",
        len(out.result.actions),
        len(out.result.skipped),
        len(out.result.backed_up),
    )
    return out
