"""refine 入口——wiki 自编译：页面自身作为输入重跑编译链，刷新关系。

用法:
    VIRTUAL_ENV= .venv/bin/python scripts/refine_wiki.py

数据流:
    wiki/{concepts,entities,topics}/*.md → CompilePipeline.ingest_one
    （index 视图排除当前页条目；不存 source 档案页）
"""

import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 项目 src 加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.compiler.pipeline import CompilePipeline
from wiki_agent.compiler.quality import format_scan_report, scan_wiki
from wiki_agent.compiler.refine import refine_all, refine_pages
from wiki_agent.log import begin_trace, configure_logging, setup_event_log
from wiki_agent.queue import QueueStore

WIKI_DIR = PROJECT_ROOT / "wiki"


async def main():
    # ── 运行容器（runs/refine_<ts>——与 compile 容器区分）──────
    run_dir = WIKI_DIR / ".logs" / "runs" / f"refine_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(file_path=str(run_dir / "run.log"))
    setup_event_log(run_dir / "events.jsonl")
    begin_trace(trace_id=f"refine_{run_dir.name}")

    cfg = load_config(project_root=PROJECT_ROOT)
    llm = create_llm(cfg.llm)
    vlm = create_vlm(cfg.vlm)

    # 统一队列——失败事项的人机接口（处理完移除，事件流仍是事实源）。
    # 路径注入（config 解析的 workspace，不重推导）
    queue = QueueStore(cfg.paths.resolved_workspace_dir())

    pages = refine_pages(WIKI_DIR)
    if not pages:
        print("无页面可 refine")
        return
    print(f"refine 输入: {len(pages)} 个页面 "
          f"(concepts/entities/topics，sources 与系统文件排除)")

    # 备份——refine 会更新页面自身，破坏性操作先留底（回滚/审计）
    backup_dir = run_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    n_backup = 0
    for page in pages:
        rel = page.relative_to(WIKI_DIR)
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(page, dst)
        n_backup += 1
    print(f"备份: {n_backup} 个页面 → backup/")

    pipeline = CompilePipeline(
        llm=llm, vlm=vlm, wiki_dir=WIKI_DIR,
        mode="refine",
    )

    # 中间结果存档——与 compile 的 artifacts 同构，靠容器前缀区分
    # （runs/refine_<ts>/artifacts/<页面slug>/{extract,analysis,plan}.json）
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    def on_page(page: Path, r) -> None:
        rel = page.relative_to(WIKI_DIR)
        print(f"  [{'✗' if isinstance(r, Exception) else '✓'}] {rel}")
        if isinstance(r, Exception):
            # 失败现场在 refine_failure 事件（raw 全量）+ 统一队列（人机接口）
            queue.append("ingest_failure",
                         source="refine", file=str(rel),
                         stage=getattr(r, "stage", None) and r.stage.value,
                         error=str(r)[:500])
            return  # 失败现场已在 refine_failure 事件（raw 全量）
        # 存档中间结果——追溯"这个页面 refine 时怎么想的"
        page_dir = artifacts_dir / str(rel).replace(".md", "").replace("/", "_")
        page_dir.mkdir(parents=True, exist_ok=True)
        if r.extract:
            (page_dir / "extract.json").write_text(
                r.extract.document_summary, encoding="utf-8")
        if r.analysis:
            (page_dir / "analysis.json").write_text(
                json.dumps({
                    "analysis_text": r.analysis.analysis_text,
                    "entities": r.analysis.entities,
                    "concepts": r.analysis.concepts,
                    "relationships": [
                        {"from": x.from_page, "to": x.to_page,
                         "relation": x.relation, "detail": x.detail}
                        for x in r.analysis.relationships
                    ],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
        if r.plan:
            (page_dir / "plan.json").write_text(
                json.dumps({"page_targets": [
                    {"wiki_path": t.wiki_path, "title": t.title,
                     "disposition": t.disposition.value, "reason": t.reason}
                    for t in r.plan.page_targets
                ], "raw": r.plan.raw}, ensure_ascii=False, indent=2),
                encoding="utf-8")

    stats = await refine_all(pipeline, pages, on_page=on_page)

    print(f"\n=== refine 完成 ===")
    print(f"  成功: {stats['ok']}  无操作: {stats['noop']}  失败: {stats['failed']}")

    # 收尾: 全库扫描报告
    issues = scan_wiki(WIKI_DIR)
    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warning"]
    print(f"  扫描: {len(errors)} 错误, {len(warns)} 警告")
    (run_dir / "scan_report.md").write_text(
        format_scan_report(issues), encoding="utf-8")
    print(f"  运行目录: {run_dir}")


if __name__ == "__main__":
    asyncio.run(main())
