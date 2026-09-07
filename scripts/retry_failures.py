"""重新执行 source 级失败队列。

用法:
    uv run python scripts/retry_failures.py
    uv run python scripts/retry_failures.py <issue_id>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from wiki_agent.application.issue_actions import IssueActionExecutor
from wiki_agent.application.runtime import AppRuntime


async def main(selected: str | None = None) -> None:
    runtime = AppRuntime.from_project_root(Path.cwd())
    executor = IssueActionExecutor(runtime)
    issue_ids = [selected] if selected else executor.retry_batch_candidates()
    if not issue_ids:
        print("没有可重试的问题。")
        return
    async with runtime:
        for issue_id in issue_ids:
            try:
                await executor.execute(issue_id, "retry")
            except Exception as exc:
                print(f"{issue_id}: failed — {type(exc).__name__}: {exc}")
            else:
                print(f"{issue_id}: succeeded")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
