"""issue_action job 的队列适配层：注册进 JobWorker；业务逻辑在 actions.IssueActionExecutor。"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from wiki_agent.issues import InvalidIssueTransitionError, IssueAlreadyClaimedError
from wiki_agent.issues.actions import IssueActionExecutor
from wiki_agent.issues.models import JsonObject
from wiki_agent.issues.retry_source import SourceUnavailableError
from wiki_agent.queue import Job, JobResult, Kind

if TYPE_CHECKING:
    from wiki_agent.queue.worker import JobWorker


class IssueActionJobHandler:
    """issue_action job 的执行体（经 register_job_handlers 进 JobWorker，适配器只做入口映射）。

    业务拒绝（来源丢失/动作非法/未实现）是终态：返回无联动语义的
    failed 结果，不因被拒绝的动作新增 issue 记录。rescan 的 issue
    终态不在这里写：job_effect 只产出判断依据，终态统一由 outcome 在
    job 终态事务写入。
    """

    def __init__(self, executor: IssueActionExecutor):
        self._executor = executor

    async def __call__(self, job: Job, progress) -> JobResult:
        try:
            # job payload 来自 json 落库，运行时即 JsonObject 形状
            detail = self._executor.job_effect(
                job.resource, job.mode, cast("JsonObject | None", job.payload), progress=progress
            )
        except (
            SourceUnavailableError,
            IssueAlreadyClaimedError,
            InvalidIssueTransitionError,
            ValueError,
        ) as exc:
            return JobResult(status="failed", detail={"error": str(exc)[:500]})
        effect_detail: dict[str, object] = {k: v for k, v in detail.items()}
        return JobResult(status="succeeded", detail=effect_detail)


def register_job_handlers(worker: JobWorker, executor: IssueActionExecutor) -> None:
    """注册 issue_action 的执行：执行体是 IssueActionJobHandler，业务逻辑在 executor。"""
    worker.register(Kind.ISSUE_ACTION, IssueActionJobHandler(executor))
