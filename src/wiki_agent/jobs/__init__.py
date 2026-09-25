"""Job 执行引擎：表与不变式（store/models/errors）、提交口、认领循环、终态联动。

契约与存储从包根直接导出；服务级对象（JobService/JobWorker/JobOutcomeHandler）
按模块路径导入，避免把 issues/sync 的依赖强加给只要 Job 行的调用方。
"""

from wiki_agent.jobs.errors import (
    DuplicateInFlightJob,
    PipelineBusy,
    RestructureInProgress,
    SyncInProgress,
)
from wiki_agent.jobs.models import WIKI_WRITE_KINDS, Job, JobResult, Kind, Settlement
from wiki_agent.jobs.store import JobStore

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
    "SyncInProgress",
]
