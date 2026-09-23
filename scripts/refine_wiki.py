"""refine 入口——逐页 refine 排队 + 自泵到队列空。

用法:
    .venv/bin/python scripts/refine_wiki.py [--wiki-dir DIR] [--limit N]

执行体在 application.wiki_ops：一页一个 job、pre-reset→ingest→
成功一页一提交；失败只撤该页的未提交改动（task failed + 事件，不进问题账本）。
批尾注支持 ``/wiki revert-batch <批id>`` 整批回撤。结构重组是另一个
入口（scripts/restructure_wiki.py），不再与 refine 同事务。

本壳与 sync 脚本同宿主模式：AppRuntime 装配、持执行锁、泵完做一次
全库质量收尾（进问题中心）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wiki_agent.application.runtime import AppRuntime
from wiki_agent.config import load_config
from wiki_agent.exec_lock import ExecutionBusy, acquire_execution_lock, release_execution_lock
from wiki_agent.issues.producers import report_quality_findings
from wiki_agent.log import configure_logging, get_logger, setup_event_log
from wiki_agent.wiki.quality import scan_wiki

logger = get_logger("REFINE_WIKI")


async def main(wiki_dir: Path | None = None, limit: int | None = None) -> None:
    configure_logging(console_level=logging.INFO)
    overrides = {"paths": {"wiki_dir": str(Path(wiki_dir).resolve())}} if wiki_dir else None
    cfg = load_config(project_root=PROJECT_ROOT, overrides=overrides)
    runtime = AppRuntime(cfg)
    setup_event_log(runtime.workspace / "logs" / "refine-events.jsonl")
    service = runtime.job_service

    acquire_execution_lock(runtime.workspace)
    try:
        jobs = service.submit_refine_batch(limit=limit)
        if not jobs:
            logger.info("没有可 refine 的页面")
            return
        batch = str(jobs[0].payload["batch"])
        logger.info("refine 入队 %d 页（批 %s）", len(jobs), batch)
        while service.store.count_in_flight() > 0:
            done = await runtime.job_worker.run_once()
            if done is None:
                break
            logger.info("  %s: %s", Path(done.resource).name, done.status)

        # 批尾全库质量收尾：只报账不撤批（一页一提交，回撤走 revert-batch）
        issues = scan_wiki(runtime.wiki_dir)
        report_quality_findings(
            runtime.issue_service,
            issues,
            origin={"mode": "refine", "trigger": "cli_refine_end"},
        )
        errors = sum(1 for i in issues if i.level == "error")
        logger.info(
            "refine 完成: %d 页（错误 %d / 警告 %d 已进问题中心）；整批回撤: /wiki revert-batch %s",
            len(jobs),
            errors,
            len(issues) - errors,
            batch,
        )
    finally:
        release_execution_lock(runtime.workspace)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.wiki_dir, args.limit))
    except ExecutionBusy as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
