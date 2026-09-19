"""重新执行 source 级失败队列。

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
from wiki_agent.log import configure_logging, get_logger

logger = get_logger("RETRY_FAILURES")


async def main(selected: str | None = None) -> None:
    # 无 run 容器——不落文件，结果直接走终端（WARNING+ 亦可由 lastResort 兜底，
    # 但 INFO 级的 succeeded/空队列提示需要显式配置才可见）。
    configure_logging(console_level=logging.INFO)
    runtime = AppRuntime.from_project_root(Path.cwd())
    executor = IssueActionExecutor(runtime)
    issue_ids = [selected] if selected else executor.retry_batch_candidates()
    if not issue_ids:
        logger.info("没有可重试的问题。")
        return
    async with runtime:
        for issue_id in issue_ids:
            try:
                await executor.execute(issue_id, "retry")
            except Exception as exc:
                logger.error("%s: failed — %s: %s", issue_id, type(exc).__name__, exc)
            else:
                logger.info("%s: succeeded", issue_id)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
