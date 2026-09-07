"""结构手术正式入口——粗提 → 复判 → 冲突消解 → 确认 → 执行。

用法:
    VIRTUAL_ENV= .venv/bin/python scripts/surgery_wiki.py             # 交互确认
    VIRTUAL_ENV= .venv/bin/python scripts/surgery_wiki.py --dry-run   # 到消解为止，不动手
    VIRTUAL_ENV= .venv/bin/python scripts/surgery_wiki.py --yes       # 跳过确认，直接执行

运行容器: runs/surgery_<ts>/{run.log, events.jsonl, backup/, pending_decisions.json}
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from wiki_agent.compiler.surgery import (
    _index_overview,
    _load_pages,
    execute,
    propose_from_index,
    re_arbitrate,
    recheck,
    resolve_conflicts,
)
from wiki_agent.config import load_config
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.producers import report_quality_findings, report_surgery_conflicts
from wiki_agent.llm.factory import create_llm
from wiki_agent.log import begin_trace, configure_logging, setup_event_log
from wiki_agent.versioning import WikiGitManager
from wiki_agent.wiki.quality import format_scan_report, scan_wiki


async def main(dry_run: bool = False, yes: bool = False):
    """手术主流程——粗提 → 复判 → 消解 → 确认 → 执行。

    Args:
        dry_run: 到消解为止，不动手。
        yes: 跳过确认，直接执行。
    """
    cfg = load_config(project_root=PROJECT_ROOT)
    wiki_dir = cfg.paths.resolved_wiki_dir().resolve()
    runs_dir = cfg.paths.resolved_runs_dir().resolve()
    run_dir = runs_dir / f"surgery_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(file_path=str(run_dir / "run.log"))
    setup_event_log(run_dir / "events.jsonl")
    begin_trace(trace_id=f"surgery_{run_dir.name}")

    print(f"═══ 1. 粗提（LLM 见全库 index，{len(_index_overview(wiki_dir).splitlines())} 页）═══")
    issue_service = IssueService(IssueStore(cfg.paths.resolved_workspace_dir()))
    llm = create_llm(cfg.llm, cfg.retry)
    proposals = await propose_from_index(llm, wiki_dir)
    if not proposals:
        print("无提议——结构健康。")
        return
    for p in proposals:
        print(f"  [{p.op}] {p.pages} — {p.reason}")

    print("\n═══ 2. 精选复判（每条带页面全文 + 引用证据）═══")
    confirmed, rejected = await recheck(llm, wiki_dir, proposals)
    for prop, reason in rejected:
        print(f"  ✗ 否决 [{prop.op}] {prop.pages} — {reason[:100]}")
    if not confirmed:
        print("复判全部否决——结构健康。")
        return
    print(f"\n确认通过 {len(confirmed)} 条:")
    for p in confirmed:
        print(f"  ✓ {p.op:18s} {p.pages} → {p.target} | {p.reason}")

    print("\n═══ 3. 依赖分析与冲突消解 ═══")
    pages = _load_pages(wiki_dir)
    clean, conflicts = resolve_conflicts(confirmed, pages)
    if conflicts:
        print(f"  确定性消解后仍冲突 {len(conflicts)} 组——LLM 复裁:")
        for c in conflicts:
            print(f"    {c.kind}: {c.detail}")
        arb = await re_arbitrate(llm, wiki_dir, conflicts)
        for p in arb.resolved:
            print(f"    复裁 → [{p.op}] {p.pages} → {p.target}")
        clean.extend(arb.resolved)
        if arb.unresolved:
            queue_file = run_dir / "pending_decisions.json"
            queue_file.write_text(
                json.dumps(
                    [
                        {
                            "kind": c.kind,
                            "detail": c.detail,
                            "proposals": [
                                {
                                    "op": p.op,
                                    "pages": p.pages,
                                    "target": p.target,
                                    "reason": p.reason,
                                }
                                for p in c.proposals
                            ],
                        }
                        for c in arb.unresolved
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            report_surgery_conflicts(
                issue_service,
                arb.unresolved,
                origin={"mode": "surgery", "run_id": run_dir.name},
            )
            print(
                f"  ⚠ {len(arb.unresolved)} 组冲突无法仲裁——"
                f"已记录 pending_decisions.json + 问题中心"
            )
    print(f"  消解后有效提议: {len(clean)} 条")
    if not clean:
        print("消解后无有效提议。")
        return

    if dry_run:
        print(f"\n(dry-run——到此为止。运行目录: {run_dir})")
        return

    if not yes:
        print("\n═══ 4. 确认（逐条 y/n）═══")
        accepted = []
        for p in clean:
            ans = input(f"  执行 [{p.op}] {p.pages}？[y/N] ").strip().lower()
            if ans == "y":
                accepted.append(p)
        if not accepted:
            print("全部跳过。")
            return
    else:
        accepted = clean
        print(f"\n═══ 4. 执行（--yes，{len(accepted)} 条全部执行）═══")

    print("\n═══ 5. 执行（备份 → 原子动作）═══")
    git_manager = WikiGitManager(wiki_dir, run_root=runs_dir)
    git_run = git_manager.begin(run_dir.name, mode="surgery")
    try:
        result = execute(wiki_dir, accepted)
    except asyncio.CancelledError as exc:
        git_manager.abort(git_run, reason=f"surgery cancelled: {exc}")
        raise
    except Exception as exc:
        git_manager.abort(git_run, reason=f"surgery exception: {exc}")
        raise

    print("\n═══ 6. 扫描报告 ═══")
    issues = scan_wiki(wiki_dir)
    report_quality_findings(
        issue_service,
        issues,
        origin={"mode": "surgery", "run_id": run_dir.name},
    )
    print(format_scan_report(issues))
    errors = [issue for issue in issues if issue.level == "error"]
    if errors or result.skipped:
        git_manager.abort(
            git_run,
            reason=f"surgery validation failed: errors={len(errors)}, skipped={len(result.skipped)}",
        )
        print("Git: 已恢复到运行前版本")
    else:
        git_manager.commit(
            git_run,
            message=f"wiki: surgery {git_run.run_id}",
            scan_report=run_dir / "scan_report.md",
            metadata={"actions": len(result.actions), "scan_errors": len(errors)},
        )
        print(f"Git: 已提交 {git_run.commit}")

    print(
        f"\n完成: {len(result.actions)} 个动作 / 跳过 {len(result.skipped)}"
        f" / 备份 {len(result.backed_up)} 文件"
    )
    if result.skipped:
        for s in result.skipped:
            print(f"  ⚠ {s}")
    print(f"运行目录: {run_dir}（备份在 backup/，日志在 run.log/events.jsonl）")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv, yes="--yes" in sys.argv))
