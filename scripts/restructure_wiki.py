"""结构重组入口。

用法:
    .venv/bin/python scripts/restructure_wiki.py             # 逐条确认后入队执行
    .venv/bin/python scripts/restructure_wiki.py --dry-run   # 只预览提议不动手
    .venv/bin/python scripts/restructure_wiki.py --yes       # 全收确认入队

分工与 sync 快照同构：提议阶段（粗提→复判→消解）在提交侧同步跑、含交互
确认；确认后切成互不依赖的执行单元入队——一单元一个 job、一单元一笔提交。
执行体在 application.wiki_ops——pre-reset→execute→scan 闸门→成功提交本
单元/失败撤销本单元；批尾注支持 ``/wiki revert-batch`` 整批回撤。
本脚本持执行锁，驱动队列到空。
"""

import asyncio
import logging
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from wiki_agent.application.restructure_service import restructure_wiki
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.config import load_config
from wiki_agent.exec_lock import ExecutionBusy, acquire_execution_lock, release_execution_lock
from wiki_agent.log import configure_logging, get_logger, setup_event_log
from wiki_agent.wiki.quality import format_scan_report, scan_wiki

logger = get_logger("RESTRUCTURE_WIKI")


async def _interactive_confirm(proposals):
    """逐条 y/N；返回被接受的子集（面向操作者的交互提示，属 stdout UX）。"""
    accepted = []
    for p in proposals:
        ans = input(f"  执行 [{p.op}] {p.pages}？[y/N] ").strip().lower()
        if ans == "y":
            accepted.append(p)
    return accepted


async def main(dry_run: bool = False, yes: bool = False) -> int:
    cfg = load_config(project_root=PROJECT_ROOT)
    runtime = AppRuntime(cfg)
    wiki_dir = runtime.wiki_dir

    if not dry_run:
        # 本进程要执行 restructure job，因此持执行锁（dry-run 纯预览不触库）
        acquire_execution_lock(runtime.workspace)
    try:
        confirm = None if (yes or dry_run) else _interactive_confirm
        # 只跑提议阶段：dry_run=True 保证 restructure_wiki 不执行手术，
        # 执行由 job 在队列里做（accepted 即人确认的结果）
        outcome = await restructure_wiki(
            runtime.agent.llm,
            wiki_dir,
            confirm=confirm,
            dry_run=True,
        )
        if outcome.healthy:
            logger.info("结构健康——无需重组。")
        else:
            logger.info(
                "提议: %d 粗提 → %d 复判确认 → %d 有效（接受 %d）",
                len(outcome.proposals),
                len(outcome.confirmed),
                len(outcome.effective),
                len(outcome.accepted),
            )
            for p in outcome.effective:
                logger.info("  %s %s → %s | %s", p.op, p.pages, p.target or "-", p.reason[:80])
        if outcome.unresolved:
            logger.warning("%d 组冲突无法自动仲裁——已丢弃不执行。", len(outcome.unresolved))
        for line in format_scan_report(scan_wiki(wiki_dir)).splitlines():
            logger.info("%s", line)

        if dry_run:
            logger.info("dry-run：未入队。")
            return 0
        if not outcome.accepted:
            logger.info("未确认任何提议——未入队。")
            return 0

        service = runtime.job_service
        jobs = service.submit_restructure([asdict(p) for p in outcome.accepted])
        batch = str(jobs[0].payload["batch"])
        logger.info("重组已入队: %d 个执行单元（批 %s）", len(jobs), batch)
        while service.count_in_flight() > 0:
            if await runtime.job_worker.run_once() is None:
                break
        rows = [service.get(job.id) for job in jobs]
        failed = [row for row in rows if row.status != "succeeded"]
        for row in failed:
            logger.warning("执行单元被撤销: %s", row.error)
        if failed:
            logger.warning(
                "重组部分失败（%d/%d 单元已提交并保留；整批回撤: /wiki revert-batch %s）",
                len(rows) - len(failed),
                len(rows),
                batch,
            )
            return 1
        logger.info("重组完成，%d 个单元各自提交（整批回撤: /wiki revert-batch %s）", len(rows), batch)
        return 0
    finally:
        if not dry_run:
            release_execution_lock(runtime.workspace)


if __name__ == "__main__":
    configure_logging(console_level=logging.INFO)
    setup_event_log(
        load_config(project_root=PROJECT_ROOT).paths.resolved_workspace_dir()
        / "logs"
        / "restructure-events.jsonl"
    )
    try:
        raise SystemExit(asyncio.run(main(dry_run="--dry-run" in sys.argv, yes="--yes" in sys.argv)))
    except ExecutionBusy as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
