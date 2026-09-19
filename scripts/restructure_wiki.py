"""结构重组正式入口（脚本壳）。

用法:
    .venv/bin/python scripts/restructure_wiki.py             # 交互逐条确认
    .venv/bin/python scripts/restructure_wiki.py --dry-run   # 只出报告不动手
    .venv/bin/python scripts/restructure_wiki.py --yes       # 跳过确认全执行

流程编排与审计在应用层 service；本壳只管 run 目录、git 事务、交互式确认，
输出统一走 logger（终端可见 + 落运行日志）。
"""

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from wiki_agent.application.restructure_service import restructure_wiki
from wiki_agent.config import load_config
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.producers import report_quality_findings
from wiki_agent.llm.factory import create_llm
from wiki_agent.log import begin_trace, configure_logging, get_logger, setup_event_log
from wiki_agent.versioning import WikiGitManager
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
    wiki_dir = cfg.paths.resolved_wiki_dir().resolve()
    runs_dir = cfg.paths.resolved_runs_dir().resolve()
    run_dir = runs_dir / f"restructure_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(console_level=logging.INFO, file_path=str(run_dir / "run.log"))
    setup_event_log(run_dir / "events.jsonl")
    begin_trace(trace_id=run_dir.name)

    issue_service = IssueService(IssueStore(cfg.paths.resolved_workspace_dir()))
    llm = create_llm(cfg.llm, cfg.retry)
    git_manager = WikiGitManager(wiki_dir, run_root=runs_dir)
    git_run = git_manager.begin(run_dir.name, mode="restructure")

    confirm = None if (yes or dry_run) else _interactive_confirm
    try:
        outcome = await restructure_wiki(
            llm,
            wiki_dir,
            confirm=confirm,
            dry_run=dry_run,
            issue_service=issue_service,
            origin={"mode": "restructure", "run_id": run_dir.name},
        )
    except asyncio.CancelledError as exc:
        git_manager.abort(git_run, reason=f"restructure cancelled: {exc}")
        raise
    except Exception as exc:
        git_manager.abort(git_run, reason=f"restructure exception: {exc}")
        raise

    issues = scan_wiki(wiki_dir)
    report_quality_findings(
        issue_service, issues, origin={"mode": "restructure", "run_id": run_dir.name}
    )
    errors = [i for i in issues if i.level == "error"]
    skipped = len(outcome.result.skipped) if outcome.result else 0
    if errors or skipped:
        git_manager.abort(
            git_run,
            reason=f"restructure validation failed: errors={len(errors)}, skipped={skipped}",
        )
        git_note = "Git: 已恢复到运行前版本"
    else:
        committed = git_manager.commit(
            git_run,
            message=f"wiki: restructure {git_run.run_id}",
            scan_report=run_dir / "scan_report.md",
            metadata={
                "actions": len(outcome.result.actions) if outcome.result else 0,
                "scan_errors": len(errors),
            },
        )
        git_note = f"Git: 已提交 {committed.commit}"

    # 摘要统一走 logger（明细审计同在 run.log）
    if outcome.healthy:
        logger.info("结构健康——无需重组。")
    elif dry_run:
        logger.info("dry-run：有效提议 %d 条（未执行）。详见 %s", len(outcome.effective), run_dir)
    elif outcome.result is None:
        logger.info("未执行（确认 0 条）。运行目录: %s", run_dir)
    else:
        r = outcome.result
        logger.info(
            "完成：%d 动作 / 跳过 %d / 备份 %d 文件",
            len(r.actions),
            len(r.skipped),
            len(r.backed_up),
        )
    if outcome.unresolved:
        logger.warning("%d 组冲突无法自动仲裁——已记入问题中心。", len(outcome.unresolved))
    for s in outcome.result.skipped if outcome.result else []:
        logger.warning("跳过: %s", s)
    for line in format_scan_report(issues).splitlines():
        logger.info("%s", line)
    # 校验未通过的 Git 回退用 warning——成功提交才 info
    logger.log(logging.INFO if not (errors or skipped) else logging.WARNING, "%s", git_note)
    logger.info("运行目录: %s（备份在 backup/，日志在 run.log/events.jsonl）", run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(dry_run="--dry-run" in sys.argv, yes="--yes" in sys.argv)))
