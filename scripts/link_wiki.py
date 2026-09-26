"""全库关联扫入口——发现型补链的手动/定期入口。

用法:
    .venv/bin/python scripts/link_wiki.py                # 全库内容页
    .venv/bin/python scripts/link_wiki.py concepts/a entities/b   # 指定页

每批维护（sync/重组）的 link 范围只覆盖波及面（产出页与消失页的入链
页面）；老页该链新页这类发现型需求由本脚本的全库扫承接。一页一个
job、一页一笔提交；执行体在 application.wiki_ops.handle_link。
本脚本持执行锁，驱动队列到空。
"""

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from wiki_agent.application.runtime import AppRuntime
from wiki_agent.config import load_config
from wiki_agent.exec_lock import ExecutionBusy, acquire_execution_lock, release_execution_lock
from wiki_agent.jobs import PipelineBusy, SyncBaselineLag
from wiki_agent.log import configure_logging, get_logger, setup_event_log

logger = get_logger("LINK_WIKI")


async def main(slugs: list[str]) -> int:
    cfg = load_config(project_root=PROJECT_ROOT)
    runtime = AppRuntime(cfg)

    service = runtime.job_service
    acquire_execution_lock(runtime.workspace)
    try:
        # 与重组同一前提：判断基于静止且追平基线的 wiki
        while service.count_in_flight() > 0:
            if await runtime.job_worker.run_once() is None:
                break
        lag = service.sync_baseline_lag()
        if lag:
            preview = "、".join(sorted(lag)[:3])
            logger.error(
                "%d 个源未同步（%s）——先运行 scripts/sync.py 追平基线，再扫链",
                len(lag),
                preview,
            )
            return 1
        try:
            jobs = service.submit_link_batch(slugs=slugs or None)
        except (PipelineBusy, SyncBaselineLag) as exc:
            logger.error("提交暂拒: %s", exc)
            return 1
        except ValueError as exc:
            logger.error("%s", exc)
            return 2
        if not jobs:
            logger.info("没有可扫描的页面。")
            return 0
        batch = str(jobs[0].payload["batch"])
        logger.info("已入队: %d 个 link job（一页一提交，批 %s）", len(jobs), batch)
        while service.count_in_flight() > 0:
            if await runtime.job_worker.run_once() is None:
                break
        rows = [service.get(job.id) for job in jobs]
        failed = [row for row in rows if row.status != "succeeded"]
        for row in failed:
            logger.warning("link 失败: %s — %s", row.payload.get("slug"), row.error)
        if failed:
            logger.warning(
                "部分失败（%d/%d 成功；整批回撤: /wiki revert-batch %s）",
                len(rows) - len(failed),
                len(rows),
                batch,
            )
            return 1
        logger.info("关联扫完成，%d 页全部处理。", len(rows))
        return 0
    finally:
        release_execution_lock(runtime.workspace)


if __name__ == "__main__":
    configure_logging(console_level=logging.INFO)
    setup_event_log(
        load_config(project_root=PROJECT_ROOT).paths.resolved_workspace_dir()
        / "logs"
        / "link-events.jsonl"
    )
    try:
        raise SystemExit(asyncio.run(main(sys.argv[1:])))
    except ExecutionBusy as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
