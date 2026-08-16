"""refine——wiki 自编译：以 wiki 页面自身为输入重跑编译链，刷新关系。

与 compile/watch 的关键差异:
- 输入 = wiki/{concepts,entities,topics}/*.md（页面自己更新自己）
- index 视图排除当前页条目——否则 search 必然选中自己 → duplicate →
  plan 按映射更新 → 用页面摘要重写页面（信息损耗）
- 不存 source 档案页（输入就是 wiki 页面，再存 = 自我复制）

时机: 全文 index 建立后（增量生成时目标页面还不存在，链接先天不充分）。
"""

from __future__ import annotations

import re
from pathlib import Path

from wiki_agent.compiler.pipeline import CompilePipeline
from wiki_agent.ingestion.data_loader import DataLoader
from wiki_agent.errors import IngestError
from wiki_agent.log import emit_event, get_logger

logger = get_logger("REFINE")

# refine 输入范围——知识页三目录（sources/ 是源档案页不参与；index 等系统文件排除）
_CONTENT_DIRS = ("concepts", "entities", "topics")


def refine_pages(wiki_dir: str | Path) -> list[Path]:
    """收集 refine 输入——wiki 下三目录的全部 .md 页面。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        排序后的页面路径列表。
    """
    wiki = Path(wiki_dir)
    pages: list[Path] = []
    for sub in _CONTENT_DIRS:
        d = wiki / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def build_index_excluding_self(wiki_dir: str | Path):
    """构造 index_reader 钩子——返回排除指定源页面条目的 index 文本。

    排除方式: 去掉含 ``[[slug]]`` 的条目行。slug = 源路径相对 wiki 去 .md。
    条目行格式: ``- [[concepts/x]] — [concept] concepts/x.md — 标题``。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        reader 钩子（接收源路径，返回排除自身后的 index 文本）。
    """
    wiki = Path(wiki_dir)

    def reader(source_path: Path) -> str:
        index_path = wiki / "index.md"
        try:
            content = index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        rel = Path(source_path).relative_to(wiki)
        slug = str(rel).replace(".md", "")
        # 逐行过滤含自身 slug 的条目——保留空行结构
        kept = [
            line for line in content.split("\n")
            if f"[[{slug}]]" not in line
        ]
        return "\n".join(kept)

    return reader


async def refine_all(
    pipeline: CompilePipeline,
    pages: list[Path],
    *,
    on_page: callable | None = None,
) -> dict:
    """逐页 refine（串行——index 读写约束，与 compile/watch 一致）。

    Args:
        pipeline: 组装好的 refine 流水线。
        pages: 待精炼页面列表。
        on_page: (page, outcome|error) 回调——入口用它打印与存档。

    Returns:
        统计 dict: {"total", "ok", "noop", "failed"}。
    """
    stats = {"total": len(pages), "ok": 0, "noop": 0, "failed": 0}
    loader = DataLoader()

    for page in pages:
        summary = loader.load([page])
        if not summary.files:
            logger.warning("  ✗ %s 加载为空，跳过", page.name)
            emit_event("refine_failure", file=page.name, stage="load",
                       error="load_empty")
            stats["failed"] += 1
            continue
        try:
            outcome = await pipeline.ingest_one(summary.files[0])
        except IngestError as e:
            logger.error("  ✗ %s [%s]: %s", page.name, e.stage.value, str(e)[:200])
            # 事件是机器通道——全量不截断（截断是给人看的习惯）
            emit_event("refine_failure", file=page.name, stage=e.stage.value,
                       error=str(e),
                       cause=type(e.cause).__name__ if e.cause else None,
                       raw=e.raw)
            stats["failed"] += 1
            if on_page:
                on_page(page, e)
            continue
        if outcome.noop:
            stats["noop"] += 1
            emit_event("refine_noop", file=page.name)
        else:
            stats["ok"] += 1
            emit_event("refine_ingested", file=page.name,
                       pages=len(outcome.pages_written))
        if on_page:
            on_page(page, outcome)

    return stats
