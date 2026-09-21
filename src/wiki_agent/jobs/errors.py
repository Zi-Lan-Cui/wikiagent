"""Job 存储层错误。"""

from __future__ import annotations


class DuplicateInFlightJob(RuntimeError):
    """同一 resource 已存在 queued/running 的在途（in-flight）Job——唯一在途约束冲突。

    resource 统一为绝对路径字符串——compile/delete/issue_retry 同族共享
    这一身份，部分唯一索引 uq_jobs_in_flight_resource 是强制点。
    """

    def __init__(self, resource: str):
        super().__init__(f"resource 已有在途 Job: {resource}")
        self.resource = resource


class SyncInProgress(RuntimeError):
    """上一次 sync 的批次还在执行——sync 互斥串行，快照不允许叠加快照。"""

    def __init__(self) -> None:
        super().__init__("源执行队列未空闲：上一次 sync/重试尚未结束")
