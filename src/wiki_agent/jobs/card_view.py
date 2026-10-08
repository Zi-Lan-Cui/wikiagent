"""任务队列卡片的展示投影，供 web 与 CLI 共用。

含 kind 与终态 stage 到界面文案的映射；issue 关联通过注入的 lookup 解析。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wiki_agent.jobs.models import Kind

if TYPE_CHECKING:
    from wiki_agent.jobs.models import Job

# Job.kind 是 str，判定处用 Kind 常量。
_KIND_LABELS: dict[str, str] = {
    "compile": "编译",
    "delete": "删除",
    "restructure": "重组",
    "link": "补链",
    "issue_action": "问题处理",
    "maintenance_preview": "整理结构分析",
}

_TERMINAL_STAGE_LABELS = {"done": "已完成", "cancelled": "已取消"}


def task_card(job: Job, issue_lookup: Callable[[str], Any]) -> dict[str, Any]:
    """把一个 Job 行投影为队列卡片。

    Args:
        job: 持久化任务行。
        issue_lookup: 按 issue_id 取关联记录（IssueRecord 或其投影），
            不存在返回 None。

    Returns:
        卡片字典（title/resource/stage/status/result 等展示字段）。
    """
    item = asdict(job)
    item["issue_id"] = job.issue_id
    item["action"] = job.mode
    # batch 用于快照批分组与按批撤销定位
    item["batch"] = str(job.payload.get("batch") or "")
    item["current_stage"] = (
        _TERMINAL_STAGE_LABELS.get(job.stage)
        or job.stage
        or ("等待执行" if job.status == "queued" else "")
    )
    issue = issue_lookup(job.issue_id) if job.issue_id else None
    if issue is not None:
        item["title"] = issue.title
        item["resource"] = str(
            issue.resource.get("path") or issue.resource.get("label") or job.resource
        )
    else:
        label = _KIND_LABELS.get(job.kind, job.kind)
        name = Path(job.resource).name if job.resource else ""
        item["title"] = f"{label} {name}".strip()
        item["resource"] = job.resource
    # restructure/link/preview 的 resource 是内部键，标题与 resource 改用声明内容
    if job.kind == Kind.RESTRUCTURE:
        raw_unit = job.payload.get("unit")
        unit: dict[str, Any] = raw_unit if isinstance(raw_unit, dict) else {}
        ins = "+".join(unit.get("in_pages") or []) or job.resource
        outs = (
            "+".join(str(p.get("slug") or "") for p in unit.get("out") or [] if isinstance(p, dict))
            or "（删除）"
        )
        item["title"] = f"重组 {ins} → {outs}"
        item["resource"] = ins
    elif job.kind == Kind.LINK:
        item["title"] = f"补链 {job.payload.get('slug') or job.resource}"
        item["resource"] = str(job.payload.get("slug") or job.resource)
    elif job.kind == Kind.MAINTENANCE_PREVIEW:
        item["title"] = "整理结构分析"
        item["resource"] = ""
    if item["status"] == "succeeded":
        item["status"] = "completed"
    if job.status == "succeeded" and issue is not None:
        item["result"] = asdict(issue)
    return item
