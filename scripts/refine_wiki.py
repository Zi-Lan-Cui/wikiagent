"""refine 入口——wiki 自编译：页面自身作为输入重跑编译链，刷新关系。

用法:
    VIRTUAL_ENV= .venv/bin/python scripts/refine_wiki.py

数据流:
    wiki/{concepts,entities,topics}/*.md → 编译流水线
    （index 视图排除当前页条目；不存 source 档案页）
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from wiki_agent.compiler.workflows.failures import SourceFailureHandler
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.compiler.workflows.refine import refine_all, refine_pages
from wiki_agent.config import load_config
from wiki_agent.exec_lock import batch_wiki_transaction
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.producers import report_quality_findings
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.log import begin_trace, configure_logging, get_logger, setup_event_log
from wiki_agent.versioning import WikiGitManager
from wiki_agent.wiki.quality import format_scan_report, scan_wiki

logger = get_logger("REFINE_WIKI")


async def main(
    wiki_dir: Path | None = None, project_root: Path = PROJECT_ROOT, limit: int | None = None
):
    """执行锁 + 在途闸圈住整个批 refine（过渡形态，③期入队后退役）。"""
    cfg = load_config(project_root=Path(project_root).resolve())
    with batch_wiki_transaction(cfg.paths.resolved_workspace_dir()):
        return await _run(wiki_dir, project_root, limit)


async def _run(
    wiki_dir: Path | None = None, project_root: Path = PROJECT_ROOT, limit: int | None = None
):
    """refine 主流程——备份 → 逐页精炼 → 扫描报告。"""
    # 运行容器（runs/refine_<ts>——与 compile 容器区分）
    project_root = Path(project_root).resolve()
    cfg = load_config(project_root=project_root)
    wiki_dir = (
        Path(wiki_dir).resolve()
        if wiki_dir is not None
        else cfg.paths.resolved_wiki_dir().resolve()
    )
    runs_dir = cfg.paths.resolved_runs_dir().resolve()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = runs_dir / f"refine_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 批协议入口：pre-reset 把残骸收敛到 HEAD（审计文件在 scope 外不受
    # 影响），本次 refine 要么整批 commit、要么 restore——不存在中间态入历史。
    git_manager = WikiGitManager(wiki_dir)
    git_manager.restore()

    configure_logging(console_level=logging.INFO, file_path=str(run_dir / "run.log"))
    setup_event_log(run_dir / "events.jsonl")
    begin_trace(trace_id=f"refine_{run_dir.name}")

    llm = create_llm(cfg.llm, cfg.retry)
    vlm = create_vlm(cfg.vlm, cfg.retry)

    issue_service = IssueService(IssueStore(cfg.paths.resolved_workspace_dir()))
    failure_handler = SourceFailureHandler(issue_service, mode="refine")

    pages = refine_pages(wiki_dir)
    if limit is not None:
        pages = pages[:limit]
    if not pages:
        logger.warning("无页面可 refine")
        return
    logger.info("refine 输入: %d 个页面 (concepts/entities/topics)", len(pages))

    pipeline = CompilePipeline(
        llm=llm,
        vlm=vlm,
        wiki_dir=wiki_dir,
        mode="refine",
        compile_config=cfg.compile,
    )

    # 中间结果存档——与 compile 的 artifacts 同构，靠容器前缀区分
    # （runs/refine_<ts>/artifacts/<页面slug>/{extract,analysis,plan}.json）
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    before_pages = {
        str(page.relative_to(wiki_dir)): page.read_text(encoding="utf-8")
        for page in pages
        if page.exists()
    }

    def on_page(page: Path, r) -> None:
        rel = page.relative_to(wiki_dir)
        if isinstance(r, Exception):
            logger.warning("[✗] %s — %s: %s", rel, type(r).__name__, r)
        else:
            logger.info("[✓] %s", rel)
        page_dir = artifacts_dir / str(rel).replace(".md", "").replace("/", "_")
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "meta.json").write_text(
            json.dumps(
                {
                    "source": str(rel),
                    "status": "failed" if isinstance(r, Exception) else "completed",
                    "error": str(r) if isinstance(r, Exception) else "",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if str(rel) in before_pages:
            (page_dir / "page_before.md").write_text(before_pages[str(rel)], encoding="utf-8")
        if isinstance(r, Exception):
            # 失败现场由 refine_all 的统一 source handler 记录；这里仍保留
            # source 和 before 快照，供 refine eval 判断是否产生半成品。
            return
        # 存档中间结果——追溯"这个页面 refine 时怎么想的"
        if page.exists():
            (page_dir / "page_after.md").write_text(
                page.read_text(encoding="utf-8"), encoding="utf-8"
            )
        if r.extract:
            (page_dir / "extract.json").write_text(r.extract.document_summary, encoding="utf-8")
        if r.search:
            (page_dir / "search.json").write_text(
                json.dumps(
                    {
                        "rel_paths": r.search.rel_paths,
                        "raw": r.search.raw,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        if r.analysis:
            (page_dir / "analyze.json").write_text(
                json.dumps(
                    {
                        "analysis_text": r.analysis.analysis_text,
                        "entities": r.analysis.entities,
                        "concepts": r.analysis.concepts,
                        "relationships": [
                            {
                                "from": x.from_page,
                                "to": x.to_page,
                                "relation": x.relation,
                                "detail": x.detail,
                            }
                            for x in r.analysis.relationships
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        if r.plan:
            (page_dir / "plan.json").write_text(
                json.dumps(
                    {
                        "page_targets": [
                            {
                                "wiki_path": t.wiki_path,
                                "title": t.title,
                                "disposition": t.disposition.value,
                                "reason": t.reason,
                            }
                            for t in r.plan.page_targets
                        ],
                        "raw": r.plan.raw,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    try:
        stats = await refine_all(
            pipeline,
            pages,
            failure_handler=failure_handler,
            on_page=on_page,
        )
    except asyncio.CancelledError:
        git_manager.restore()
        raise
    except Exception:
        git_manager.restore()
        raise

    logger.info(
        "refine 完成: 成功 %d 无操作 %d 失败 %d", stats["ok"], stats["noop"], stats["failed"]
    )

    # 收尾: 全库扫描报告
    issues = scan_wiki(wiki_dir)
    report_quality_findings(
        issue_service,
        issues,
        origin={"mode": "refine", "run_id": run_dir.name},
    )
    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warning"]
    logger.info("扫描: %d 错误, %d 警告", len(errors), len(warns))
    (run_dir / "scan_report.md").write_text(format_scan_report(issues), encoding="utf-8")
    if errors:
        git_manager.restore()
        logger.warning("Git: 已恢复到运行前版本（scan 存在 %d error）", len(errors))
    else:
        commit = git_manager.commit_all(
            f"wiki: refine {run_id}",
            body=f"stats: {stats}, scan_warnings: {len(warns)}",
        )
        if commit:
            logger.info("Git: 已提交 %s", commit[:8])
        else:
            logger.info("Git: 无变更未提交")
    logger.info("运行目录: %s", run_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki-dir", type=Path, default=None)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.wiki_dir, args.project_root, args.limit))
