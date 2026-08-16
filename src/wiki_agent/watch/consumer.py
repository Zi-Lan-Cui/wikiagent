"""消费端——单 worker 从队列取任务，串行执行。

串行是硬需求: ingest 与 sources 清理都读写 wiki（index.md / sources/），
并发会互相覆盖（幽灵 index 的教训）。队列吸收突发，worker 一个一个来。

队列元素两种:
    ("ingest", Path)   — 文件变更，走 ingest_one
    ("delete", name)   — 源文件删除，走 sources 页清理（本模块实现）

源文件删除清理规则（纯代码确定性，无 LLM）:
    - 页面 sources 只含被删文件 → 删除页面 + 全库正文引用换别名
    - 页面 sources 还含其他文件 → 保留页面，仅移除该条目
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from wiki_agent.compiler.parse import split_frontmatter
from wiki_agent.compiler.pipeline import CompilePipeline
from wiki_agent.ingestion.data_loader import DataLoader
from wiki_agent.errors import IngestError
from wiki_agent.log import emit_event, get_logger
from wiki_agent.watch.state import WatchState

logger = get_logger("WATCH_CONSUMER")


# frontmatter 切分统一走 parse.split_frontmatter（全项目唯一实现）


def _clean_body_links(wiki: Path, slug: str) -> int:
    """全库正文中 [[sources/<slug>|别名]] → 别名纯文本。

    Args:
        wiki: wiki 根目录。
        slug: 被删 sources 页 slug。

    Returns:
        清理的文件数。
    """
    changed = 0
    for sub in ("concepts", "entities", "topics"):
        d = wiki / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            content = p.read_text(encoding="utf-8")
            new_content = re.sub(
                rf"\[\[sources/{re.escape(slug)}(?:\|([^\]]+?))?\]\]",
                lambda m: m.group(1) if m.group(1) else slug.rsplit("/", 1)[-1],
                content,
            )
            if new_content != content:
                p.write_text(new_content, encoding="utf-8")
                changed += 1
                logger.info("  正文引用清理: %s", p.name)
    return changed


class WatchConsumer:
    """watch 模式消费者——queue → 任务分发 → 状态回写。"""

    def __init__(
        self,
        queue: asyncio.Queue,
        pipeline: CompilePipeline,
        state: WatchState,
        *,
        wiki_dir: str | Path,
    ):
        self._queue = queue
        self._pipeline = pipeline
        self._state = state
        self._wiki_dir = Path(wiki_dir)

    async def run(self) -> None:
        """主循环——取任务、按类型分发、回写状态。"""
        logger.info("consumer 启动（单 worker 串行）")
        loader = DataLoader()
        while True:
            item = await self._queue.get()
            try:
                kind = item[0] if isinstance(item, tuple) else "ingest"
                if kind == "delete":
                    self._process_delete(item[1])
                else:
                    await self._process_ingest(item if not isinstance(item, tuple) else item[1], loader)
            finally:
                self._queue.task_done()

    def _process_delete(self, name: str) -> None:
        """源文件删除 → sources 页清理（wiki 写操作，消费者职责）。

        Args:
            name: 被删源文件名。
        """
        wiki = self._wiki_dir
        src_dir = wiki / "sources"
        if not src_dir.is_dir():
            return
        for page in sorted(src_dir.glob("*.md")):
            content = page.read_text(encoding="utf-8")
            fm, _ = split_frontmatter(content)
            raw_sources = fm.get("sources", "")
            listed = [s.strip().strip("\"'")
                      for s in raw_sources.strip("[]").split(",") if s.strip()]
            if name not in listed:
                continue
            remaining = [s for s in listed if s != name]
            slug = page.stem
            if remaining:
                # 规则 3: 保留页面，移除条目
                new_sources = ", ".join(f'"{s}"' for s in remaining)
                new_content = re.sub(
                    r'(?m)^\s*sources\s*:.*$',
                    f"sources: [{new_sources}]",
                    content, count=1,
                )
                page.write_text(new_content, encoding="utf-8")
                action = f"keep {slug}（sources 移除 {name}）"
            else:
                # 规则 2: 只剩被删文件 → 删除页面 + 正文引用换别名
                page.unlink()
                cleaned = _clean_body_links(wiki, slug)
                action = f"delete {slug}（清理 {cleaned} 处正文引用）"
            logger.info("  %s", action)
            emit_event("watch_source_deleted", file=name, action=action)

    async def _process_ingest(self, path: Path, loader: DataLoader) -> None:
        """处理文件变更——加载 → ingest → 状态回写。

        Args:
            path: 变更的文件路径。
            loader: DataLoader 实例。
        """
        name = path.name
        logger.info("ingest: %s", name)

        # 单文件加载——复用 DataLoader 的属性提取与模态判定
        summary = loader.load([path])
        if not summary.files:
            logger.warning("  跳过 %s: 加载为空", name)
            emit_event("watch_skipped", file=name, reason="load_empty")
            return
        raw_file = summary.files[0]

        try:
            outcome = await self._pipeline.ingest_one(raw_file)
        except IngestError as e:
            logger.error("  ingest 失败 [%s]: %s", e.stage.value, str(e)[:200])
            # 事件是机器通道——全量不截断（截断是给人看的习惯）
            emit_event("watch_failure", file=name, stage=e.stage.value,
                       error=str(e),
                       cause=type(e.cause).__name__ if e.cause else None,
                       raw=e.raw)
            return

        # 状态回写: ingest 完成才更新（失败保留 pending，下次变更再触发）
        st = self._state.get(str(path))
        if outcome.noop:
            emit_event("watch_noop", file=name)
        else:
            emit_event("watch_ingested", file=name,
                       pages=len(outcome.pages_written))
        st.last_ingested_at = ""
        self._state.set(str(path), st)
        self._state.save()
        logger.info("  ✓ %s 完成 (%d 页面)", name, len(outcome.pages_written))
