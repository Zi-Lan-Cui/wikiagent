"""队列机制包：表与不变式（store/models/errors）、行生命周期与互斥原语（queue）、
认领循环（worker）、终态联动（outcomes）、共享写协议（wiki_write）。

契约与存储从包根直接导出；服务级对象（JobQueue/JobWorker/JobOutcomeHandler）
按模块路径导入，避免把联动依赖强加给只要 Job 行的调用方。
"""

from wiki_agent.queue.errors import (
    DuplicateInFlightJob,
    PipelineBusy,
    RestructureInProgress,
    SyncBaselineLag,
    SyncInProgress,
)
from wiki_agent.queue.models import WIKI_WRITE_KINDS, Job, JobResult, Kind, Settlement
from wiki_agent.queue.store import JobStore

__all__ = [
    "WIKI_WRITE_KINDS",
    "DuplicateInFlightJob",
    "Job",
    "JobResult",
    "JobStore",
    "Kind",
    "PipelineBusy",
    "RestructureInProgress",
    "Settlement",
    "SyncBaselineLag",
    "SyncInProgress",
]
