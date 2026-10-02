"""结构维护流程编排：调用 LLM 前的预检、提议、整批入队。

命令层只做参数解析与结果格式化；流程在本模块，返回结构化结果，
输出文案由命令处理。

另实现 jobs.MaintenancePlanner 协议：单元校验与批尾补链目标计算需要读
wiki 当前内容与 restructure 声明模型（compiler），属于 compiler 的知识，
不放在通用的 jobs 提交入口。组装根把本实现注入 JobService，jobs 不 import compiler。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki_agent.application.restructure_service import (
    MaintenanceOutcome,
    propose_maintenance,
)
from wiki_agent.compiler.content_pages import all_content_slugs
from wiki_agent.compiler.restructure import (
    Unit,
    UnitError,
    assert_units_valid,
    pages_linking_to,
)
from wiki_agent.jobs import Job, PipelineBusy, SyncBaselineLag
from wiki_agent.jobs.service import MaintenancePlan


def gate_text(job_service: Any) -> str | None:
    """/maintain 与 /link 共用的预检：有在途任务或基线落后时返回暂拒文案。

    提交入口（JobService._raise_if_maintenance_blocked）在事务内还会复查
    同一判定——这里先挡住注定失败的提交，避免在其上消耗一次 LLM 分析。
    """
    if job_service.wiki_write_in_flight() > 0:
        return "写 wiki 的任务有在途，等当前批到终态后再执行。"
    lag = job_service.sync_baseline_lag()
    if lag:
        preview = "、".join(sorted(lag)[:3])
        return f"{len(lag)} 个源未同步（{preview}）。请先 /compile（快照 sync）追平基线。"
    return None


class MaintainFlow:
    """run_maintain 的结构化结果——命令按字段格式化。"""

    def __init__(self) -> None:
        self.outcome: MaintenanceOutcome | None = None
        self.jobs: list[Job] = []
        self.blocked: str | None = None
        self.error: str | None = None
        self.submit_rejected: str | None = None


async def run_maintain(
    llm: Any, wiki_dir: Path, job_service: Any, *, dry_run: bool
) -> MaintainFlow:
    """预检 → 提议 →（非 dry-run 且有可执行单元）整批入队。"""
    flow = MaintainFlow()
    blocked = gate_text(job_service)
    if blocked:
        flow.blocked = blocked
        return flow
    try:
        flow.outcome = await propose_maintenance(llm, wiki_dir)
    except Exception as exc:
        flow.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return flow
    outcome = flow.outcome
    if dry_run or outcome.healthy or not outcome.effective:
        return flow
    try:
        flow.jobs = job_service.submit_maintenance([u.to_dict() for u in outcome.accepted])
    except UnitError as exc:
        flow.submit_rejected = f"入队拒绝——单元与盘面不符: {exc}"
    except (PipelineBusy, SyncBaselineLag) as exc:
        # RestructureInProgress/SyncInProgress 是 PipelineBusy 子类，不必点名
        flow.submit_rejected = f"提交暂拒——{exc}。"
    return flow


class MaintenancePlannerImpl:
    """实现 jobs.MaintenancePlanner：维护批的单元校验与批尾补链规划。"""

    def plan_maintenance(self, units: list[dict], wiki_dir: Path) -> MaintenancePlan:
        """基于 wiki 当前内容重跑消解规则：解析→校验→算批尾补链目标。

        损坏声明与违例单元都抛 UnitError（提交入口拒绝绕过消解的清单）；
        补链目标 = 全部产出页 ∪ 消失页的入链页（wiki 内容不变时该集合确定）。
        """
        try:
            parsed = [Unit.from_dict(raw) for raw in units]
        except (TypeError, ValueError) as exc:
            raise UnitError(f"单元声明损坏: {exc}") from exc
        assert_units_valid(parsed, set(all_content_slugs(wiki_dir)))
        if not parsed:
            return MaintenancePlan(units=[], link_slugs=[])
        vanished = sorted({s for unit in parsed for s in unit.vanished})
        link_slugs = sorted(
            {p for unit in parsed for p in unit.out_slugs}
            | set(pages_linking_to(wiki_dir, vanished))
        )
        return MaintenancePlan(
            units=[unit.to_dict() for unit in parsed], link_slugs=link_slugs
        )

    def resolve_link_targets(self, slugs: list[str] | None, wiki_dir: Path) -> list[str]:
        """全库扫描的补链目标：None=全部内容页；给定 slug 不在内容页列表则抛 ValueError。"""
        roster = all_content_slugs(wiki_dir)
        if slugs is None:
            return list(roster)
        unknown = [s for s in dict.fromkeys(slugs) if s not in set(roster)]
        if unknown:
            raise ValueError(f"不是可维护的 wiki 页: {unknown}")
        return list(dict.fromkeys(slugs))
