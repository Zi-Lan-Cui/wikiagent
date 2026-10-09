"""Job 终态联动的注册位与分发编排，不含任何业务规则。

由 complete_with_outcome 在终态事务内调用 apply(job, result, conn)，所有跨表写
并入该事务。文件侧动作 SyncState 与档案页进不了 SQLite 事务——规则返回 commit
后要执行的动作清单，由队列在 commit 后立即执行：先写库再写文件，崩溃时靠
recover_stale 与 digest 幂等重做收敛。

规则本体由各业务线在自己的包内提供，装配根按注入面接线
（application/outcome_registry）。分发只认 handler 申报的 settlement
与 error_type；未申报、未知值或无注册规则都不动作，未知申报记警告——申报了
不存在的类别是错误，账本不能按它联动。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Protocol

from wiki_agent.log import get_logger
from wiki_agent.queue import Job, JobResult, Settlement

logger = get_logger("JOB_OUTCOMES")


class SettlementRule(Protocol):
    """成功联动：在终态事务 conn 内写业务账，返回 commit 后执行的动作清单。

    settlement 是分发依据；register_on_success 注册的规则收到 None——
    文件侧结算不依赖 handler 申报类别。
    """

    def on_succeeded(
        self,
        job: Job,
        result: JobResult,
        settlement: Settlement | None,
        conn: sqlite3.Connection,
    ) -> list[Callable[[], None]]: ...


class ErrorRule(Protocol):
    """失败联动：在终态事务 conn 内记账，返回 commit 后执行的动作清单。"""

    def on_error(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> list[Callable[[], None]]: ...


class JobOutcomeHandler:
    """终态副作用的唯一分发点：按注册表把结果交给各线规则。"""

    def __init__(self) -> None:
        self._settlements: dict[Settlement, list[SettlementRule]] = {}
        self._any_success: list[SettlementRule] = []
        self._errors: dict[str, list[ErrorRule]] = {}

    def register_settlement(self, settlement: Settlement, rule: SettlementRule) -> None:
        self._settlements.setdefault(settlement, []).append(rule)

    def register_on_success(self, rule: SettlementRule) -> None:
        """任何成功的 job 都触发本规则；分发时 settlement 传 None。"""
        self._any_success.append(rule)

    def register_error(self, error_type: str, rule: ErrorRule) -> None:
        if not error_type.strip():
            raise ValueError("error_type 不能为空")
        self._errors.setdefault(error_type, []).append(rule)

    def coverage(self) -> tuple[set[Settlement], set[str], bool]:
        """只读注册面：已注册类别、error_type 与有无任意成功规则。

        供契约测试断言装配注册完整；运行期分发不消费它。
        """
        return set(self._settlements), set(self._errors), bool(self._any_success)

    def apply(
        self, job: Job, result: JobResult, conn: sqlite3.Connection
    ) -> list[Callable[[], None]]:
        """把 result 的联动并入 conn 事务；返回 commit 后要执行的动作。

        cancelled 与未注册的 error_type 不产生动作。
        """
        post_commit: list[Callable[[], None]] = []
        if result.status == "succeeded":
            for rule in self._any_success:
                post_commit += rule.on_succeeded(job, result, None, conn)
            settlement = self._settlement_of(job, result)
            if settlement is None:
                return post_commit  # 未申报即只有"任意成功"规则参与
            for rule in self._settlements.get(settlement, []):
                post_commit += rule.on_succeeded(job, result, settlement, conn)
        elif result.error_type:
            for rule in self._errors.get(result.error_type, []):
                post_commit += rule.on_error(job, result, conn)
        return post_commit

    @staticmethod
    def _settlement_of(job: Job, result: JobResult) -> Settlement | None:
        raw = str(result.detail.get("settlement") or "")
        if not raw:
            return None
        try:
            return Settlement(raw)
        except ValueError:
            logger.warning("job %s 申报了未知结算类别 %r，账本不动", job.id, raw)
            return None
