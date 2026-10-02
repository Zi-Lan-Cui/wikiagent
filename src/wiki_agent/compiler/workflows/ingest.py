"""单文件编译流水线：批量编译与 sync 执行共用的入口。

一个源文件 → convert → chunk → extract → search → analyze → plan →
execute → index。流水线不决定失败策略：阶段异常携带阶段信息，
统计与存档由调用方完成。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from wiki_agent.compiler.extraction import Extractor
from wiki_agent.compiler.integration import compile_integrator
from wiki_agent.compiler.models import (
    AnalysisResult,
    ExtractResult,
    IntegrationPlan,
    SearchResult,
    SourceChunk,
    SourceDocument,
)
from wiki_agent.config import CompileConfig
from wiki_agent.documents.chunkers import Chunker, StructuredChunker, TextChunker
from wiki_agent.documents.converters import Converter, MinerUConverter
from wiki_agent.documents.loader import RawFileProperties
from wiki_agent.errors import IngestError, IngestStage, WikiAgentError
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import get_logger, span
from wiki_agent.wiki.pages import index_line, slug_from_ref

T = TypeVar("T")

logger = get_logger("COMPILE_PIPELINE")

_DEFAULT_CHUNK_SIZE = 8_000
_DEFAULT_MODEL_CONTEXT = 120_000
_DEFAULT_EXTRACT_CONCURRENCY = 3


@dataclass
class IngestOutcome:
    """单文件 ingest 的产出——调用方据此存档/统计。"""

    source: str
    """源文件名。"""

    converted_chars: int = 0
    extract: ExtractResult | None = None
    search: SearchResult | None = None
    analysis: AnalysisResult | None = None
    plan: IntegrationPlan | None = None
    pages_written: list[str] = field(default_factory=list)
    """实际落盘的页面相对路径（execute 返回的 targets）。"""

    noop: bool = False
    """plan 空 targets——合法无操作（内容已被现有页面覆盖）。"""


class CompilePipeline:
    """组装 convert→chunk→extract→integrate 并串行处理单个文件。

    compile_folder 与 SourceJobHandler 各持一个实例——组件（LLM/VLM 客户端）
    构造一次，跨文件复用。
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        vlm,
        wiki_dir: str | Path,
        source_records_dir: str | Path | None = None,
        chunk_size: int | None = None,
        model_context: int | None = None,
        extract_concurrency: int | None = None,
        compile_config: CompileConfig | None = None,
        on_progress: Callable[[str], None] | None = None,
    ):
        """初始化流水线：convert→chunk→extract→search→analyze→plan→execute。

        Args:
            llm: LLM 客户端。
            vlm: VLM 客户端（图片 caption）。
            wiki_dir: wiki 根目录。
            source_records_dir: 工作区中的来源摘要存档目录。
            chunk_size: 分块大小（字符）。
            model_context: extract 阶段的模型上下文窗口。
            extract_concurrency: extract 并发数。
            compile_config: 编译预算配置。
            on_progress: 阶段切换回调，接收稳定的英文阶段代码。
        """
        self._wiki_dir = Path(wiki_dir)
        self._source_records_dir = (
            Path(source_records_dir) if source_records_dir is not None else None
        )
        self._on_progress = on_progress
        budget = compile_config or CompileConfig()
        chunk_size = budget.chunk_size if chunk_size is None else chunk_size
        model_context = budget.context_window if model_context is None else model_context
        extract_concurrency = (
            budget.extract_concurrency if extract_concurrency is None else extract_concurrency
        )

        from wiki_agent.compiler import prompts as prompts_pkg

        self._prompts = prompts_pkg.compile

        self._converter = Converter(
            converters=[
                MinerUConverter(
                    backend="pipeline",
                    llm=vlm,
                    caption_images=True,
                    assets_dir=self._wiki_dir / "assets",
                ),
            ]
        )
        self._chunker = Chunker(
            [
                StructuredChunker(),
                TextChunker(max_chunk_size=chunk_size),
            ]
        )
        self._extractor = Extractor(
            llm,
            model_context=model_context,
            max_concurrency=extract_concurrency,
            source_records_dir=self._source_records_dir,
            save_source_page=True,
            prompts=self._prompts,
            system_tokens=budget.extract_system_tokens,
            output_tokens=budget.extract_output_tokens,
            safety_buffer=budget.context_safety_buffer,
        )
        self._integrator = compile_integrator(llm, wiki_dir=self._wiki_dir)

    async def ingest_one(self, raw_file: RawFileProperties) -> IngestOutcome:
        """单文件完整流水线。

        整个文件包一个 span("ingest_file")——事件流里能看到
        每个文件的耗时与成败（llm_attempt 只覆盖调用级）。

        Args:
            raw_file: 源文件属性。

        Returns:
            产出（统计/存档用）。

        Raises:
            IngestError: 阶段失败，stage 指明失败发生在哪一段。
        """
        async with span("ingest_file", file=raw_file.name):
            return await self._ingest_one(raw_file)

    async def _ingest_one(self, raw_file: RawFileProperties) -> IngestOutcome:
        outcome = IngestOutcome(source=raw_file.name)

        # 1. Convert
        cf = await self._stage(IngestStage.CONVERT, raw_file.name, self._converter.convert, raw_file)
        outcome.converted_chars = len(cf.content)
        logger.info("  [%s] → %d chars, %d 图片", cf.ext, len(cf.content), cf.content.count("!["))

        # 转换为空时 source 未进入编译链：必须在调用 LLM 前失败，
        # 不能让空内容经全文替补成 chunk 后产生标题驱动的页面
        if not cf.content.strip():
            raise IngestError(
                IngestStage.CONVERT,
                "转换后内容为空，禁止调用 LLM。",
                source=raw_file.name,
                error_code="empty_converted_content",
            )

        # 2. Chunk（chunk 列表为空时以全文作为唯一 chunk）
        ck_list = self._chunker.chunk(cf)
        if not ck_list:
            logger.warning("  Chunk 为空，用全文兜底")
            ck_list = [_FallbackChunk(content=cf.content)]

        # 3. Extract
        sd = SourceDocument(
            name=cf.name,
            ext=cf.ext,
            path=str(cf.path),
            chunks=[_to_source_chunk(ck, len(ck_list), cf.name) for ck in ck_list],
        )
        outcome.extract = await self._stage(
            IngestStage.EXTRACT, raw_file.name, self._extractor.extract, sd
        )
        logger.info("  摘要: %d chars", len(outcome.extract.document_summary))

        # 非空 source 得到空摘要属于抽取失败；低信息但有事实的摘要
        # 仍可进入 plan，由 plan 决定是否无操作
        if not outcome.extract.document_summary.strip():
            raise IngestError(
                IngestStage.EXTRACT,
                "Extractor 返回空摘要，禁止进入 plan/execute。",
                source=raw_file.name,
                error_code="empty_extract_summary",
            )

        # 4. Search → Analyze → Plan（analyze/plan 内部已 raise IngestError）
        # index 存在性只在入口保证一次，后续环节读到的总是真实或新建的空 index
        self._ensure_index()
        index_content = (self._wiki_dir / "index.md").read_text(encoding="utf-8")
        schema = self._read_optional("schema.md")
        purpose = self._read_optional("purpose.md")

        # 契约: ingest_one 只抛 IngestError。search/analyze/plan 的未预期
        # 异常经 _stage 包装，IngestError 原样透传（保留 stage）
        search_result = await self._stage(
            IngestStage.SEARCH, raw_file.name, self._integrator.search,
            outcome.extract, index_content,
        )
        logger.info("  search: %d 个候选", len(search_result.rel_paths))
        outcome.search = search_result
        outcome.analysis = await self._stage(
            IngestStage.ANALYZE, raw_file.name, self._integrator.analyze,
            outcome.extract, search_result,
        )
        outcome.plan = await self._stage(
            IngestStage.PLAN,
            raw_file.name,
            self._integrator.plan,
            outcome.extract,
            outcome.analysis,
            schema=schema,
            purpose=purpose,
            index_content=index_content,
        )

        # 5. Execute + index 更新（execute 失败隔离在页级，这里失败是批级问题）
        n = len(outcome.plan.page_targets)
        if n == 0:
            outcome.noop = True
            logger.info("  plan: 无页面操作")
            return outcome
        try:
            executed = await self._stage(
                IngestStage.EXECUTE, raw_file.name, self._integrator.execute,
                outcome.plan, outcome.extract,
            )
            outcome.pages_written = [
                t.wiki_path for t in executed
                if (self._wiki_dir / _normalize(t.wiki_path)).exists()
            ]
        except IngestError:
            # execute 部分成功时，已落盘页面仍要进 index：否则磁盘有页、
            # index 无条目，后续编译无法命中，重试也修不回（index 只追加不重建）
            written = [
                t.wiki_path
                for t in outcome.plan.page_targets
                if (self._wiki_dir / _normalize(t.wiki_path)).exists()
            ]
            if written:
                self._append_index(outcome.plan, written)
            raise

        self._append_index(outcome.plan, outcome.pages_written)
        return outcome

    async def _stage(
        self, stage: IngestStage, source: str, fn: Callable[..., Awaitable[T]], *args, **kwargs
    ) -> T:
        """阶段调用包装：报告进度并统一异常分类。

        IngestError 原样透传（保留 stage），WikiAgentError 转译为
        IngestError，其余包装为未分类；调用方只需处理一种异常。
        """
        self._notify_progress(stage)
        try:
            return await fn(*args, **kwargs)
        except IngestError:
            raise
        except WikiAgentError as e:
            raise IngestError(stage, str(e), source=source, cause=e) from e
        except Exception as e:
            raise IngestError(stage, f"未分类: {e}", source=source, cause=e) from e

    def _notify_progress(self, stage: IngestStage) -> None:
        callback = getattr(self, "_on_progress", None)
        if callback is not None:
            callback(stage.value)

    def _ensure_index(self) -> None:
        """index 存在性保证：缺失（新库首跑/被删）时创建空文件。

        显式初始化而非读时吞异常：读路径保持严格，创建只在本步骤发生。
        """
        index_path = self._wiki_dir / "index.md"
        if not index_path.exists():
            index_path.write_text("", encoding="utf-8")
            logger.info("  index.md 不存在——已创建空 index")

    def _read_optional(self, name: str) -> str:
        """读取 wiki 系统文件。

        Args:
            name: 文件名（schema.md/purpose.md）。

        Returns:
            文件内容（strip 后）；不存在返回空串。
        """
        try:
            return (self._wiki_dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _append_index(self, plan: IntegrationPlan, pages_written: list[str]) -> None:
        """新页面进 index：只登记实际落盘的页面。

        index 已由 _ensure_index 保证存在，这里读失败即 bug，不吞。

        Args:
            plan: 集成计划（取 target 的 slug/标题）。
            pages_written: 实际落盘的页面路径列表。
        """
        index_path = self._wiki_dir / "index.md"
        existing = index_path.read_text(encoding="utf-8")
        # pages_written 带 .md 后缀（normalize 后），与 slug 比对前先归一
        written_slugs = {slug_from_ref(p) for p in pages_written}
        fresh: list[str] = []
        for pt in plan.page_targets:
            slug = slug_from_ref(pt.wiki_path)
            if f"[[{slug}]]" in existing or slug not in written_slugs:
                continue
            from wiki_agent.wiki.frontmatter import parse_frontmatter

            fm = parse_frontmatter(self._wiki_dir / _normalize(pt.wiki_path))
            page_type = fm.get("type", "")
            title = fm.get("title", pt.title)
            summary = fm.get("summary", "")
            goal = fm.get("goal", "")
            # summary 说明已有内容，goal 说明页面边界和职责；二者都给
            # search/analyze 看，正文仍不进入 index，避免索引膨胀。
            summary = " ".join(str(summary).splitlines())
            goal = " ".join(str(goal).splitlines())
            fresh.append(index_line(slug, str(page_type), str(title), summary, goal))
        if fresh:
            index_path.write_text(
                existing.rstrip() + "\n" + "\n".join(fresh) + "\n",
                encoding="utf-8",
            )
            logger.info("  index: +%d 条目", len(fresh))


class _FallbackChunk:
    """chunk 列表为空时的替补：单 chunk 装全文。"""

    def __init__(self, content: str):
        self.content = content
        self.chunk_index = 0


def _to_source_chunk(ck, total: int, source_name: str) -> SourceChunk:
    """ingestion chunk → compiler 模型：标题路径取自 chunker metadata。

    chunk 的 heading 归属在切分时确定、由 metadata 携带，消费端不
    重新解析。source_name 由调用方传入，用于 chunk 级摘要的出处标注。

    Args:
        ck: ingestion chunk 对象。
        total: chunk 总数。
        source_name: 源文件名（Extract prompt 的出处标注）。

    Returns:
        compiler 侧 SourceChunk。
    """
    meta = getattr(ck, "metadata", None) or {}
    return SourceChunk(
        content=ck.content,
        index=ck.chunk_index,
        total=total,
        heading_path=meta.get("heading_path", ""),
        source_name=source_name,
    )


def _normalize(wiki_path: str) -> str:
    """去掉 wiki/ 前缀（integrate 内部已规范化，这里防 LLM 路径变体）。

    Args:
        wiki_path: 原始路径。

    Returns:
        去掉前缀后的相对路径。
    """
    p = wiki_path.strip()
    while p.startswith("wiki/"):
        p = p[len("wiki/") :]
    return p
