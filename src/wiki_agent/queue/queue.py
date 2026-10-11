"""队列机制：行生命周期与互斥原语，不含业务判定。

行只由 complete_with_outcome 推终态：jobs 行与跨表联动同事务、带 CAS。
互斥用闸原语是 in_flight_wiki_write_counts 与 pipeline_busy——业务线在
自己的提交事务内取用原语决定挡不挡，机制不提供"替线判闸"的入口。
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from wiki_agent.errors import ERROR_SUMMARY_LIMIT, summarize_error
from wiki_agent.log import emit_event, get_logger
from wiki_agent.queue import WIKI_WRITE_KINDS, Job, JobResult, JobStore, PipelineBusy
from wiki_agent.snapshots import SnapshotStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from wiki_agent.queue.outcomes import JobOutcomeHandler

logger = get_logger("JOB_QUEUE")

# detail 中的大字段：仅供 outcomes 同事务内消费（写完成记录、issue 记账、档案页
# 落盘），不持久化进 jobs 行——否则 state.db 随每轮 sync 膨胀、list() 反复反序列化大对象
_NON_PERSISTED_RESULT_KEYS = ("text", "source_page", "archive_ops", "raw")


def _persistable_result(detail: dict) -> dict:
    return {k: v for k, v in detail.items() if k not in _NON_PERSISTED_RESULT_KEYS}


class JobQueue:
    """jobs 表的执行机制：入队、认领、终态 CAS、超时回收与互斥计数。"""

    def __init__(
        self,
        *,
        store: JobStore,
        snapshots: SnapshotStore,
        outcomes: JobOutcomeHandler,
        on_submit: Callable[[], None] | None = None,
    ):
        self.store = store
        self.snapshots = snapshots
        self.outcomes = outcomes
        # 新任务入队后的回调，装配根接线为 worker 唤醒；不传则维持纯轮询
        self.on_submit = on_submit
        self.recovered_jobs = 0
        # 清扫崩溃遗留的无引用快照目录，保留非终态任务引用的批
        self.snapshots.sweep_orphans(self.store.in_flight_batch_ids())

    def recover_stale(self) -> int:
        """把超时残留的 running 行回队——仅执行锁持有者调用。

        不在构造期自动跑：构造队列对象不代表持有执行锁，第二进程构造期回队
        会与正在执行的 job 双写 wiki。装配根与脚本在持锁后显式调用。
        """
        self.recovered_jobs = self.store.recover_stale()
        return self.recovered_jobs

    def in_flight_wiki_write_counts(
        self, _conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """写 wiki 各 kind 的在途行数，互斥闸的判定依据。"""
        counts = self.store.in_flight_kinds(_conn=_conn)
        return {kind: n for kind, n in counts.items() if kind in WIKI_WRITE_KINDS}

    @staticmethod
    def pipeline_busy(busy: dict[str, int]) -> PipelineBusy:
        """在途集合非空时的拒绝对象；kind 用中文标签计数，面向终端用户。"""
        names = {"compile": "同步", "delete": "清理", "restructure": "整理", "link": "补全链接"}
        parts = "、".join(f"{names.get(kind, kind)} {n} 个" for kind, n in sorted(busy.items()))
        return PipelineBusy(f"存在进行中的整理任务（{parts}），请等待其完成")

    def submit(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        issue_id: str = "",
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        job = self.store.enqueue(
            kind=kind,
            resource=resource,
            mode=mode,
            payload=payload,
            idempotency_key=idempotency_key,
            issue_id=issue_id,
            _conn=_conn,
        )
        # _conn 路径的提交在调用方线程内同步走完，worker 无从半路认领；
        # 外层事务若回滚，多余唤醒只是空跑一趟
        if self.on_submit is not None:
            self.on_submit()
        return job

    # 执行生命周期（Worker 独占）

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        return self.store.claim_next(kinds=kinds)

    def mark_stage(self, job_id: str, stage: str) -> Job:
        # 刷新 updated_at：recover_stale 依据它判超时
        return self.store.update(job_id, stage=stage)

    def complete_with_outcome(self, job: Job, result: JobResult) -> Job:
        """唯一终态提交点：jobs 行与联动同事务。

        带 CAS（仅 running 可翻转）：行已被取代或取消时迟到写静默跳过——不联动、
        不改 issue，返回行的现状。SyncState 与溯源档案页的文件写在提交之后。失败
        即终态，不产生后继 job。
        """
        with self.store.transaction(immediate=True) as conn:
            won = self.store.try_finalize(
                job.id,
                status=result.status,
                stage={"succeeded": "done", "cancelled": "cancelled"}.get(result.status),
                error=(
                    summarize_error(result.detail.get("error"), ERROR_SUMMARY_LIMIT)
                    if result.status == "failed"
                    else None
                ),
                result=_persistable_result(result.detail),
                _conn=conn,
            )
            if not won:
                return self.store.get(job.id, _conn=conn)
            post_commit = self.outcomes.apply(job, result, conn)
        # 文件动作逐条隔离：一条抛错不能带走其余动作，更不能击穿 worker 循环；
        # 账不落由 recover_stale 与 digest 幂等重做收敛，故障必须可见
        for action in post_commit:
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                logger.error("job %s 的 post_commit 动作失败: %s", job.id, exc, exc_info=True)
                emit_event("outcome_action_failed", job_id=job.id, error=str(exc)[:300])
        # 批内最后一个任务进终态后删除该批快照目录
        batch = str(job.payload.get("batch") or "")
        if batch and self.store.count_in_flight_for_batch(batch) == 0:
            try:
                self.snapshots.drop_batch(batch)
            except Exception as exc:  # noqa: BLE001
                logger.error("批 %s 快照目录清理失败: %s", batch, exc)
        return self.store.get(job.id)

    def cancel_terminal(self, job: Job) -> None:
        """进程取消路径：以 CAS 将行置为 cancelled。

        提交时未改业务账状态，取消无需回转任何关联记录。
        """
        self.store.try_finalize(job.id, status="cancelled", stage="cancelled")

    # 只读视图

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.store.list(limit=limit)

    def get(self, job_id: str) -> Job:
        """按 id 读一行；不存在时抛 LookupError。"""
        return self.store.get(job_id)

    def count_in_flight(self) -> int:
        """在途（queued/running）行数——脚本驱动队列到空的判据。"""
        return self.store.count_in_flight()

    def in_flight_issue_ids(self) -> set[str]:
        """有关联 queued/running job 的 issue id 集合。"""
        return set(self.store.open_issue_ids_with_in_flight_job())

    def wiki_write_in_flight(self) -> int:
        """写 wiki 的在途 job 数，供 /wiki revert 前检查（WIKI_WRITE_KINDS）。"""
        return self.store.in_flight_for_kinds(WIKI_WRITE_KINDS)
