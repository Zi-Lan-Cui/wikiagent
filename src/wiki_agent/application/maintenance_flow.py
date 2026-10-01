"""结构维护 CLI 用例——花钱前预检、提议、整批入队的流程编排。

原先整段流程长在 agent/commands 的 MaintainCommand 里（agent 包反向
import application，靠函数级导入遮环）；命令层只应解析参数与格式化
结果。本模块收编流程，返回结构化结果，文案归命令。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki_agent.application.restructure_service import (
    MaintenanceOutcome,
    propose_maintenance,
)
from wiki_agent.compiler.restructure import UnitError
from wiki_agent.jobs import Job, PipelineBusy, SyncBaselineLag


def gate_text(job_service: Any) -> str | None:
    """/maintain 与 /link 共用的花钱前预检：在途或基线落后返回暂拒文案。

    提交口（JobService._raise_if_maintenance_blocked）在事务内还会复查
    同一判定——这里只把注定失败的提交挡在 LLM 分析之前。
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
