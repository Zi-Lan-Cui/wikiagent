"""Job 存储层与提交闸口的错误。"""

from __future__ import annotations


class DuplicateInFlightJob(RuntimeError):
    """同一 resource 已存在 queued/running 的在途（in-flight）Job——唯一在途约束冲突。

    resource 统一为绝对路径字符串——compile/delete/issue_retry 同族共享
    这一身份，部分唯一索引 uq_jobs_in_flight_resource 是强制点。
    """

    def __init__(self, resource: str):
        super().__init__(f"resource 已有在途 Job: {resource}")
        self.resource = resource


class PipelineBusy(RuntimeError):
    """写 wiki 的任务有在途，本次提交被暂拒——当前批到达终态后可重试。

    提交层互斥的统一异常：跨阶段与 refine 式的自撞都报它，sync 与
    restructure 的同族自撞由子类给出专门语义。执行层不感知这个概念，
    判定只发生在提交口。
    """

    def __init__(self, message: str = "写 wiki 的任务未空闲：等当前批到达终态后再提交") -> None:
        super().__init__(message)


class SyncInProgress(PipelineBusy):
    """上一次 sync 的批次还在执行——sync 互斥串行，快照不允许叠加快照。"""

    def __init__(self) -> None:
        super().__init__("源执行队列未空闲：上一次 sync/重试尚未结束")


class RestructureInProgress(PipelineBusy):
    """一批重组还没有全部到终态——同一时刻只允许一个重组批次。

    批内各单元按依赖顺序排队生效，第二笔批次的单元混进来会失去
    "撤销这一批"的清晰边界。
    """

    def __init__(self) -> None:
        super().__init__("重组批次未空闲：等当前批全部到终态后再提交")


class SyncBaselineLag(RuntimeError):
    """同步基线落后：存在未同步（且未挂失败账）的源，提交被暂拒。

    与 PipelineBusy 分家——"队列在忙"和"基线没追平"是两种原因，
    补救动作不同（等批完成 vs 先 sync），消息不能互相冒充。
    挂 open/blocked 编译失败账的源属隔离区、不算落后（失败即保持脏
    是账本语义，不该卡死批操作）。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
