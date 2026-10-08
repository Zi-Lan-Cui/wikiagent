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

    def __init__(self, message: str = "写 wiki 的任务未空闲：等当前批到达终态后再提交") -> None:
        super().__init__(message)


class SyncInProgress(PipelineBusy):
    """上一次 sync 批还在执行：sync 互斥串行，不接受叠加快照。"""

    def __init__(self) -> None:
        super().__init__("源执行队列未空闲：上一次 sync/重试尚未结束")


class RestructureInProgress(PipelineBusy):
    """一批重组未全部到终态：同一时刻只允许一个重组批次。

    批内单元按依赖顺序排队生效，另一批的单元混入会破坏按批撤销的边界。
    """

    def __init__(self) -> None:
        super().__init__("重组批次未空闲：等当前批全部到终态后再提交")


class SyncBaselineLag(RuntimeError):
    """同步基线落后：存在未同步且无关联失败记录的源，提交被拒。

    与 PipelineBusy 分列，原因与补救不同：PipelineBusy 是等批完成，本类需先 sync。
    挂 open/blocked 编译失败记录的源不计入落后，失败保持脏属正常，
    不应因此阻塞批操作。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
