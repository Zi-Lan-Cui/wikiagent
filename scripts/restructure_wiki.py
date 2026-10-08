"""结构维护入口。

用法:
    .venv/bin/python scripts/restructure_wiki.py             # 逐条确认后入队执行
    .venv/bin/python scripts/restructure_wiki.py --dry-run   # 只预览提议不动手
    .venv/bin/python scripts/restructure_wiki.py --yes       # 全收确认入队

分工与 sync 快照同构：提议阶段（初步建议→二次确认→消解）在提交侧同步跑、
含交互确认；确认后整批入队——一单元一个 job、一单元一笔提交，link 任务
自动排在批尾（范围 = 全部产出页 ∪ 消失页的入链页面）。执行体在
application.wiki_ops——核对→路由→装配→逐页成文→落盘收尾→扫描闸门→
提交本单元；失败只撤本单元。批尾注支持 ``/wiki revert-batch`` 整批回撤。
本脚本持执行锁，驱动队列到空。
"""

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from wiki_agent.application.pump import drain_queue
from wiki_agent.application.restructure_service import propose_maintenance
from wiki_agent.application.runtime import AppRuntime
from wiki_agent.config import load_config
from wiki_agent.exec_lock import ExecutionBusy, acquire_execution_lock, release_execution_lock
from wiki_agent.jobs import PipelineBusy, RestructureInProgress, SyncBaselineLag
from wiki_agent.log import configure_logging, get_logger, setup_event_log
from wiki_agent.wiki.quality import format_scan_report, scan_wiki

logger = get_logger("RESTRUCTURE_WIKI")


async def _interactive_confirm(units):
    """逐条 y/N 确认，返回被接受的子集；交互提示面向操作者，走 stdout。"""
    accepted = []
    for unit in units:
        pair = f"{'+'.join(unit.in_pages)} → {'+'.join(unit.out_slugs) or '（删除）'}"
        ans = input(f"  执行 {pair}（{unit.reason[:60]}）？[y/N] ").strip().lower()
        if ans == "y":
            accepted.append(unit)
    return accepted


async def main(dry_run: bool = False, yes: bool = False) -> int:
    cfg = load_config(project_root=PROJECT_ROOT)
    runtime = AppRuntime(cfg)
    wiki_dir = runtime.wiki_dir

    service = runtime.job_service
    if not dry_run:
        # 本进程要执行 restructure/link job，需持执行锁；dry-run 纯预览不触库，不持锁
        acquire_execution_lock(runtime.workspace)
    try:
        if not dry_run:
            # 提议必须基于静止的 wiki：崩溃遗留的在途任务先泵空再开始分析
            await drain_queue(service, runtime.job_worker)
        # 主闸在 LLM 分析之前，dry-run 同样被闸：基线落后的提议没有执行价值
        lag = service.sync_baseline_lag()
        if lag:
            preview = "、".join(sorted(lag)[:3])
            logger.error(
                "%d 个源未同步（%s）——先运行 scripts/sync.py 追平基线，再重组",
                len(lag),
                preview,
            )
            return 1
        confirm = None if (yes or dry_run) else _interactive_confirm
        outcome = await propose_maintenance(runtime.agent.llm, wiki_dir, confirm=confirm)
        if outcome.healthy:
            logger.info("结构健康——无需重组。")
        else:
            logger.info(
                "提议: %d 初提 → %d 复核保留 → %d 有效（接受 %d）",
                len(outcome.proposed),
                len(outcome.confirmed),
                len(outcome.effective),
                len(outcome.accepted),
            )
        for unit, reason in outcome.rejected + outcome.dropped:
            logger.info("  放弃 %s — %s", "+".join(unit.in_pages), reason[:80])
        for unit in outcome.effective:
            out = "+".join(unit.out_slugs) or "（删除）"
            logger.info("  执行 %s → %s | %s", "+".join(unit.in_pages), out, unit.reason[:80])
        for line in format_scan_report(scan_wiki(wiki_dir)).splitlines():
            logger.info("%s", line)

        if dry_run:
            logger.info("dry-run：未入队。")
            return 0
        if not outcome.accepted:
            logger.info("未确认任何提议——未入队。")
            return 0

        try:
            jobs = service.submit_maintenance([u.to_dict() for u in outcome.accepted])
        except (PipelineBusy, RestructureInProgress, SyncBaselineLag) as exc:
            logger.error("提交暂拒: %s", exc)
            return 1
        except Exception as exc:  # UnitError 等：声明落不上当前盘面
            logger.error("入队拒绝: %s", str(exc)[:300])
            return 1
        batch = str(jobs[0].payload["batch"])
        n_units = sum(1 for j in jobs if j.kind == "restructure")
        logger.info(
            "已入队: %d 个单元 + %d 个补链（批 %s）", n_units, len(jobs) - n_units, batch
        )
        await drain_queue(service, runtime.job_worker)
        rows = [service.get(job.id) for job in jobs]
        failed = [row for row in rows if row.status != "succeeded"]
        for row in failed:
            logger.warning("任务失败: %s — %s", row.kind, row.error)
        if failed:
            logger.warning(
                "部分失败（%d/%d 成功；整批回撤: /wiki revert-batch %s）",
                len(rows) - len(failed),
                len(rows),
                batch,
            )
            return 1
        logger.info("完成，%d 个任务全部成功（整批回撤: /wiki revert-batch %s）", len(rows), batch)
        return 0
    finally:
        if not dry_run:
            release_execution_lock(runtime.workspace)


if __name__ == "__main__":
    configure_logging(console_level=logging.INFO)
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    yes = "--yes" in args
    unknown = [a for a in args if a not in ("--dry-run", "--yes")]
    if unknown:
        print(f"参数不识别: {unknown}（可用: --dry-run, --yes）")
        raise SystemExit(2)
    setup_event_log(
        load_config(project_root=PROJECT_ROOT).paths.resolved_workspace_dir()
        / "logs"
        / "restructure-events.jsonl"
    )
    try:
        raise SystemExit(asyncio.run(main(dry_run=dry_run, yes=yes)))
    except ExecutionBusy as exc:
        logging.getLogger("RESTRUCTURE_WIKI").error("%s", exc)
        raise SystemExit(1) from exc
