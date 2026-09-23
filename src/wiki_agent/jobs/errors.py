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


class RestructureInProgress(RuntimeError):
    """一批结构重组还没有全部到终态——同一时刻只允许一个重组批次。

    与 SyncInProgress 同一条理由：批内各执行单元按依赖顺序排队生效，
    第二笔批次的单元混进来会失去"撤销这一批"的清晰边界。
    """

    def __init__(self) -> None:
        super().__init__("结构重组批次未空闲：等当前批全部到终态后再提交")
