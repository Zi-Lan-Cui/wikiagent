"""refine 入口——wiki 自编译：页面自身作为输入重跑编译链，刷新关系。

用法:
    VIRTUAL_ENV= .venv/bin/python scripts/refine_wiki.py

数据流:
    wiki/{concepts,entities,topics}/*.md → CompilePipeline.ingest_one
    （index 视图排除当前页条目；不存 source 档案页）
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# 项目 src 加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wiki_agent.compiler.wiki.quality import format_scan_report, scan_wiki
from wiki_agent.compiler.workflows.failures import SourceFailureHandler
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.compiler.workflows.refine import refine_all, refine_pages
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.log import begin_trace, configure_logging, setup_event_log
from wiki_agent.queue import QueueStore
from wiki_agent.versioning import WikiGitManager

WIKI_DIR = PROJECT_ROOT / "wiki"


async def main(
    wiki_dir: Path = WIKI_DIR, project_root: Path = PROJECT_ROOT, limit: int | None = None
):
    """refine 主流程——备份 → 逐页精炼 → 扫描报告。"""
    # ── 运行容器（runs/refine_<ts>——与 compile 容器区分）──────
    wiki_dir = Path(wiki_dir).resolve()
    project_root = Path(project_root).resolve()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = wiki_dir / ".logs" / "runs" / f"refine_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Git dirty 检查必须发生在本次 run.log/events.jsonl 创建之前；否则
    # refine 自己的审计文件会被误判为用户修改。
    git_manager = WikiGitManager(wiki_dir, run_root=wiki_dir / ".logs" / "runs")
    git_run = git_manager.begin(run_id, mode="refine")

    configure_logging(file_path=str(run_dir / "run.log"))
    setup_event_log(run_dir / "events.jsonl")
    begin_trace(trace_id=f"refine_{run_dir.name}")

    cfg = load_config(project_root=project_root)
    llm = create_llm(cfg.llm, cfg.retry)
    vlm = create_vlm(cfg.vlm, cfg.retry)

    # 统一队列——失败事项的人机接口（处理完移除，事件流仍是事实源）。
    # 路径注入（config 解析的 workspace，不重推导）
    queue = QueueStore(cfg.paths.resolved_workspace_dir())
    failure_handler = SourceFailureHandler(queue, mode="refine")

    pages = refine_pages(wiki_dir)
    if limit is not None:
        pages = pages[:limit]
    if not pages:
        print("无页面可 refine")
        return
    print(f"refine 输入: {len(pages)} 个页面 (concepts/entities/topics，sources 与系统文件排除)")

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
        print(f"  [{'✗' if isinstance(r, Exception) else '✓'}] {rel}")
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
    except asyncio.CancelledError as exc:
        git_manager.abort(git_run, reason=f"refine cancelled: {exc}")
        raise
    except Exception as exc:
        git_manager.abort(git_run, reason=f"refine exception: {exc}")
        raise

    print("\n=== refine 完成 ===")
    print(f"  成功: {stats['ok']}  无操作: {stats['noop']}  失败: {stats['failed']}")

    # 收尾: 全库扫描报告
    issues = scan_wiki(wiki_dir)
    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warning"]
    print(f"  扫描: {len(errors)} 错误, {len(warns)} 警告")
    (run_dir / "scan_report.md").write_text(format_scan_report(issues), encoding="utf-8")
    if errors:
        git_manager.abort(git_run, reason=f"scan errors: {len(errors)}")
        print("  Git: 已恢复到运行前版本（scan 存在 error）")
    else:
        git_manager.commit(
            git_run,
            message=f"wiki: refine {git_run.run_id}",
            scan_report=run_dir / "scan_report.md",
            metadata={"stats": stats, "scan_errors": len(errors), "scan_warnings": len(warns)},
        )
        print(f"  Git: 已提交 {git_run.commit}")
    print(f"  运行目录: {run_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki-dir", type=Path, default=WIKI_DIR)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.wiki_dir, args.project_root, args.limit))
