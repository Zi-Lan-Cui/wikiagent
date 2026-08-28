"""重新执行 source 级失败队列。

用法:
    uv run python scripts/retry_failures.py
    uv run python scripts/retry_failures.py <queue_id>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wiki_agent.compiler.workflows.retry import retry_source_failures
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.queue import QueueStore


async def main(selected: str | None = None) -> None:
    cfg = load_config(project_root=PROJECT_ROOT)
    result = await retry_source_failures(
        QueueStore(cfg.paths.resolved_workspace_dir()),
        llm=create_llm(cfg.llm, cfg.retry),
        vlm=create_vlm(cfg.vlm, cfg.retry),
        wiki_dir=PROJECT_ROOT / "wiki",
        compile_config=cfg.compile,
        retry_config=cfg.retry,
        item_id=selected,
    )
    for item in result["results"]:
        print(f"{item['id']}: {item['status']}")
    if result.get("rolled_back"):
        print("本次重试未提交，Wiki 已回撤，队列项保留。")
    elif result.get("committed"):
        print("本次重试已提交。")
    elif result.get("message"):
        print(result["message"])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
