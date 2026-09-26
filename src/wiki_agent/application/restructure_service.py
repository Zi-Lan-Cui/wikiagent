"""维护单元的提议编排——脚本与命令共用的提议流程。

四步：初步建议（全库大纲）→ 逐组二次确认 → 冲突消解（一页只进一个
单元，代码规则）。本模块只产出可入队的单元清单与放弃明细；入队与执行
在 jobs 层（JobService.submit_maintenance）——提议与确认发生在提交侧，
job 只做执行。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wiki_agent.compiler.content_pages import all_content_slugs
from wiki_agent.compiler.restructure import (
    Unit,
    propose_units,
    recheck_units,
    resolve_unit_conflicts,
)
from wiki_agent.log import get_logger

logger = get_logger("MAINTENANCE")

ConfirmCallback = Callable[[list[Unit]], Awaitable[list[Unit]]]


@dataclass
class MaintenanceOutcome:
    proposed: list[Unit] = field(default_factory=list)
    confirmed: list[Unit] = field(default_factory=list)
    rejected: list[tuple[Unit, str]] = field(default_factory=list)  # 二次确认放弃
    dropped: list[tuple[Unit, str]] = field(default_factory=list)  # 消解拒绝
    effective: list[Unit] = field(default_factory=list)  # 消解后、可入队
    accepted: list[Unit] = field(default_factory=list)  # confirm 回调过滤后
    healthy: bool = False  # 无建议或复核全部放弃——结构无需动手
    # 注意区分：消解全部拒绝时 healthy 为 False（代码否掉的，不是"结构好"）


async def propose_maintenance(
    llm: Any,
    wiki_dir: str | Path,
    *,
    confirm: ConfirmCallback | None = None,
) -> MaintenanceOutcome:
    """跑一遍提议流程。返回结构化结果；调用方据此渲染并提交入队。"""
    wiki_dir = Path(wiki_dir)
    out = MaintenanceOutcome()

    out.proposed = await propose_units(llm, wiki_dir)
    logger.info("初步建议: %d 个单元", len(out.proposed))
    if not out.proposed:
        out.healthy = True
        logger.info("无建议——结构健康。")
        return out

    out.confirmed, out.rejected = await recheck_units(llm, wiki_dir, out.proposed)
    for unit, reason in out.rejected:
        logger.info("二次确认放弃 %s — %s", unit.in_pages, reason[:100])
    if not out.confirmed:
        out.healthy = True
        logger.info("二次确认全部放弃——结构健康。")
        return out

    clean, dropped = resolve_unit_conflicts(out.confirmed, set(all_content_slugs(wiki_dir)))
    out.dropped = dropped
    for unit, reason in dropped:
        logger.warning("消解拒绝 %s — %s", unit.in_pages, reason[:100])
    out.effective = clean
    if not clean:
        logger.info("消解后无有效单元（原因见上）——不判定为结构健康。")
        return out

    out.accepted = await confirm(out.effective) if confirm is not None else list(out.effective)
    return out
