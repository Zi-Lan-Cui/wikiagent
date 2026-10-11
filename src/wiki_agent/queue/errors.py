"""Job 存储层与提交互斥的错误类型。"""

from __future__ import annotations


class DuplicateInFlightJob(RuntimeError):
    """同一 resource 已有 queued/running 的在途 Job，违反唯一在途约束。

    resource 统一为绝对路径字符串，compile/delete/issue_retry 共用；
    由部分唯一索引 uq_jobs_in_flight_resource 强制。
    """

    def __init__(self, resource: str):
        super().__init__(f"resource 已有在途 Job: {resource}")
        self.resource = resource


class PipelineBusy(RuntimeError):
    """写 wiki 的任务在途，本次提交被拒；当前批到终态后可重试。

    提交层互斥的统一异常，sync 与 restructure 的同族冲突由子类给出专门语义。
    判定只发生在提交口，执行层不涉及。
    """

    def __init__(self, message: str = "整理任务正在进行，请稍后重试") -> None:
        super().__init__(message)


class SyncInProgress(PipelineBusy):
    """上一次 sync 批还在执行：sync 互斥串行，不接受叠加快照。"""

    def __init__(self) -> None:
        super().__init__("上一次同步尚未完成，请等待其结束")


class RestructureInProgress(PipelineBusy):
    """一批重组未全部到终态：同一时刻只允许一个重组批次。

    批内单元按依赖顺序排队生效，另一批的单元混入会破坏按批撤销的边界。
    """

    def __init__(self) -> None:
        super().__init__("当前整理批次尚未完成，请等待全部任务结束后再提交")


class SyncBaselineLag(RuntimeError):
    """同步基线落后导致的提交拒绝。

    与 PipelineBusy 分列，原因与补救不同：PipelineBusy 是等批完成，本类需先 sync。
    落后集合的判定公式在 sync 线（SyncBaseline），机制层只承载拒绝语义。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
