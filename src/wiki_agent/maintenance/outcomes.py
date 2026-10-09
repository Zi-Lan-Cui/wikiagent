"""维护线结算规则：PREVIEWED 成功即把建议清单写入提议账本。

入账与 job 终态同事务；建议清单缺失只写警告日志，不影响任务终态——
分析失败不动账本，旧 pending 只在新结果入账时被作废。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from wiki_agent.log import get_logger
from wiki_agent.maintenance.store import ProposalStore
from wiki_agent.queue import Job, JobResult, Settlement
from wiki_agent.queue.outcomes import JobOutcomeHandler

logger = get_logger("MAINTENANCE_OUTCOMES")


class _ProposalOutcomeRule:
    """账本写入的唯一路径；payload 与分析结果同构。"""

    def __init__(self, proposals: ProposalStore):
        self._proposals = proposals

    def on_succeeded(
        self,
        job: Job,
        result: JobResult,
        settlement: Settlement | None,
        conn: sqlite3.Connection,
    ) -> list[Callable[[], None]]:
        if settlement is None:
            return []
        payload = result.detail.get("preview")
        if not isinstance(payload, dict):
            logger.warning("job %s 申报 PREVIEWED 但无 preview 载荷，账本未记", job.id)
            return []
        self._proposals.record(job.id, payload, _conn=conn)
        return []


def register_maintenance_outcomes(outcomes: JobOutcomeHandler, proposals: ProposalStore) -> None:
    """PREVIEWED 挂到提议账本；离线无账本时不注册，分发器对未注册类别不动作。"""
    outcomes.register_settlement(Settlement.PREVIEWED, _ProposalOutcomeRule(proposals))
