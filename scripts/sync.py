"""手动快照同步：拍 materials 现状入队，本进程充当 worker 泵到队列空。

用法:
    uv run python scripts/sync.py            # 配置的 materials 目录
    uv run python scripts/sync.py <dir>      # 指定源目录
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from wiki_agent.application.runtime import AppRuntime
from wiki_agent.exec_lock import ExecutionBusy, acquire_execution_lock, release_execution_lock
from wiki_agent.issues.producers import report_quality_findings
from wiki_agent.jobs import SyncInProgress
from wiki_agent.log import configure_logging, get_logger, setup_event_log
from wiki_agent.wiki.quality import scan_wiki

logger = get_logger("SYNC")


async def main(source_dir: str | None = None) -> None:
    configure_logging(console_level=logging.INFO)
    runtime = AppRuntime.from_project_root(Path.cwd())
    service = runtime.job_service
    target = Path(source_dir).resolve() if source_dir else runtime.materials_dir
    setup_event_log(runtime.workspace / "logs" / "sync-events.jsonl")

    # 本进程充当 worker 泵：执行锁保证与 web/批脚本不同时写 wiki
    acquire_execution_lock(runtime.workspace)
    try:
        try:
            jobs = service.submit_sync(target)
        except SyncInProgress:
            logger.info("上一次批次仍在执行，继续泵而不是拍新快照…")
            jobs = []
        if jobs:
            logger.info("快照入队 %d 个任务（源目录 %s）", len(jobs), target)
        elif not service.store.count_in_flight():
            logger.info("无待同步变更（账本与磁盘一致）")

        worker = runtime.job_worker
        while service.store.count_in_flight() > 0:
            done = await worker.run_once()
            if done is None:
                break
            logger.info("  %s: %s", Path(done.resource).name, done.status)

        pending = service.store.count_in_flight()
        if pending:
            logger.info("仍有 %d 个任务未领取（再跑一次本脚本继续泵）", pending)

        # 批尾全库质量收尾（原批编译的最后一步随入口合并搬进 sync 宿主）：
        # 纯代码检查、按指纹合并进 issue 中心——web 侧对应"重新扫描"按钮。
        issues = scan_wiki(runtime.wiki_dir)
        report_quality_findings(
            runtime.issue_service,
            issues,
            origin={"mode": "sync", "trigger": "cli_sync_end"},
        )
        errors = sum(1 for i in issues if i.level == "error")
        warnings = len(issues) - errors
        logger.info("质量扫描: %d 错误, %d 警告（进问题中心）", errors, warnings)
        logger.info("同步结束。失败问题查看: web 问题中心 或 /queue")
    finally:
        release_execution_lock(runtime.workspace)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
    except ExecutionBusy as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("\n中断——已入队任务保留在 jobs 表，重跑本脚本继续泵。")
