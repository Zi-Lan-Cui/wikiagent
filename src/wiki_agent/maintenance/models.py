"""维护提议账本的数据契约：提议行与处置状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

Payload = dict[str, object]  # 提议内容，与分析结果的 preview 同构
Disposition = dict[str, object]  # 处置明细：submitted 记录由此产生的 job_ids


class Resolution(StrEnum):
    """提议的处置状态；三种终态都只从 pending 经 CAS 转入。"""

    PENDING = "pending"
    DISMISSED = "dismissed"  # 人工否决
    SUBMITTED = "submitted"  # 采纳并已入队执行
    SUPERSEDED = "superseded"  # 新一轮分析结果产出，本条作废


ALLOWED_RESOLUTION_TRANSITIONS: dict[Resolution, frozenset[Resolution]] = {
    Resolution.PENDING: frozenset(
        {Resolution.DISMISSED, Resolution.SUBMITTED, Resolution.SUPERSEDED}
    ),
}


class ProposalNotFoundError(LookupError):
    """提议 id 不存在。"""


class ProposalAlreadyResolvedError(RuntimeError):
    """提议已被处置——重复处置或并发修改，复查后可解。"""


@dataclass(frozen=True, slots=True)
class Proposal:
    """一条持久化的整理提议；一次分析产出一条，payload 为建议清单。"""

    id: str
    job_id: str
    title: str
    resolution: Resolution
    created_at: str
    updated_at: str
    resolved_at: str = ""
    payload: Payload = field(default_factory=dict)
    disposition: Disposition = field(default_factory=dict)
