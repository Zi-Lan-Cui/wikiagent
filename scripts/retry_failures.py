"""重新执行 source 级失败队列——统一执行模型下：排队 compile job 并驱动到空。

用法:
    uv run python scripts/retry_failures.py
    uv run python scripts/retry_failures.py <issue_id>
"""

from __future__ import annotations

import asyncio
import logging
import sys

from wiki_agent.application.issue_actions import IssueActionExecutor
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.exec_lock import ExecutionBusy, acquire_execution_lock, release_execution_lock
from wiki_agent.jobs import PipelineBusy
from wiki_agent.jobs.retry_source import SourceUnavailableError
from wiki_agent.log import configure_logging, get_logger

logger = get_logger("RETRY_FAILURES")


async def main(selected: str | None = None) -> None:
    # 结果直接走终端：INFO 级提示需要显式配置才可见
    configure_logging(console_level=logging.INFO)
    runtime = AppRuntime.from_project_root()
    service = runtime.job_service
    # 本进程充当 worker 泵：执行锁保证与 web/批脚本不同时写 wiki
    acquire_execution_lock(runtime.workspace)
    try:
        executor = IssueActionExecutor(runtime)
        issue_ids = [selected] if selected else executor.retry_batch_candidates()
        if not issue_ids:
            logger.info("没有可重试的问题。")
            return

        outcomes: dict[str, str] = {}
        try:
            jobs = service.submit_issue_retry_batch(issue_ids)
        except (PipelineBusy, SourceUnavailableError) as exc:
            logger.error("本批未提交（整批原子拒绝）：%s", exc)
            return
        for job in jobs:
            logger.info("%s: 已排队 %s", job.issue_id or job.resource, job.id)

        # 本脚本驱动队列，直到领不到本进程注册类型的任务为止
        worker = runtime.job_worker
        while service.count_in_flight() > 0:
            job = await worker.run_once()
            if job is None:
                break  # 在途行都是本进程未注册 handler 的 kind，如 issue_action
            outcomes[_issue_of(job.id, service)] = job.status
        if service.count_in_flight() > 0:
            logger.info(
                "仍有 %d 个在途任务不属于本进程的执行类型，留给注册了对应 handler 的进程",
                service.count_in_flight(),
            )

        for issue_id in issue_ids:
            status = outcomes.get(issue_id, "unknown")
            log = logger.info if status == "succeeded" else logger.error
            log("%s: %s", issue_id, status)
    finally:
        release_execution_lock(runtime.workspace)


def _issue_of(job_id: str, service) -> str:
    job = service.get(job_id)
    return job.issue_id or job.resource


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
    except ExecutionBusy as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logger.info("retry 中断，已排队的 job 留在队列中，再跑一次本脚本或 web 的 worker 会执行")
