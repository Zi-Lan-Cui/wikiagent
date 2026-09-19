"""重新执行 source 级失败队列——统一执行模型下：排队 compile job 并驱动到空。

用法:
    uv run python scripts/retry_failures.py
    uv run python scripts/retry_failures.py <issue_id>
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from wiki_agent.application.issue_actions import IssueActionExecutor
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.issues import IssueAlreadyClaimedError
from wiki_agent.jobs import DuplicateActiveJob
from wiki_agent.log import configure_logging, get_logger

logger = get_logger("RETRY_FAILURES")


async def main(selected: str | None = None) -> None:
    # 无 run 容器——不落文件，结果直接走终端（WARNING+ 亦可由 lastResort 兜底，
    # 但 INFO 级的 succeeded/空队列提示需要显式配置才可见）。
    configure_logging(console_level=logging.INFO)
    runtime = AppRuntime.from_project_root(Path.cwd())
    service = runtime.job_service
    executor = IssueActionExecutor(runtime)
    issue_ids = [selected] if selected else executor.retry_batch_candidates()
    if not issue_ids:
        logger.info("没有可重试的问题。")
        return

    from wiki_agent.compiler.workflows.retry import SourceUnavailableError

    outcomes: dict[str, str] = {}
    for issue_id in issue_ids:
        try:
            job = service.submit_issue_retry(issue_id)
        except (IssueAlreadyClaimedError, DuplicateActiveJob):
            outcomes[issue_id] = "skipped（已有在途任务）"
        except SourceUnavailableError as exc:
            outcomes[issue_id] = f"unavailable — {exc}"
        else:
            logger.info("%s: 已排队 %s", issue_id, job.id)

    # 本脚本即临时 worker：驱动到没有到期任务为止（transient 退避链未到
    # 期的部分留给后台进程，报告标注在途）
    worker = runtime.job_worker
    while service.store.count_active() > 0:
        job = await worker.run_once()
        if job is None:
            break  # 只剩 next_run_at 未到期的链式行
        outcomes[_issue_of(job.id, service)] = job.status
    if service.store.count_active() > 0:
        logger.info("仍有 %d 个退避重排在途，交给后台 worker", service.store.count_active())

    for issue_id in issue_ids:
        status = outcomes.get(issue_id, "unknown")
        log = logger.info if status in ("succeeded", "skipped（已有在途任务）") else logger.error
        log("%s: %s", issue_id, status)


def _issue_of(job_id: str, service) -> str:
    job = service.store.get(job_id)
    return job.issue_id or job.resource


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
    except KeyboardInterrupt:
        logger.info("retry 中断，已排队的 job 留在库中由后台 worker 续跑")
