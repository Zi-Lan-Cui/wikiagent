"""单文件编译流水线——批量编译与 sync 消费共用的领域入口。

一个源文件 → convert → chunk → extract → search → analyze → plan →
execute → index。阶段失败带阶段信息冒出，流水线不决定失败策略，
只报告失败发生在哪一段；调用方拿到结果自行统计与存档。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from wiki_agent.compiler.extraction import Extractor
from wiki_agent.compiler.integration import compile_integrator, refine_integrator
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

    compile_folder 与 sync 消费者各持一个实例——组件（LLM/VLM 客户端）
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
        mode: str = "compile",
        index_reader: Callable[[Path], str] | None = None,
        save_sources: bool | None = None,
        on_progress: Callable[[str], None] | None = None,
    ):
        """初始化流水线。

        mode: "compile"（源→wiki 初次编译，默认）/ "refine"（wiki 自编译）。
        模式声明一处、下游隐式推导:
        - refine: index 排除自身（无 index_reader 时构造默认排除闭包）+
          不存 source 页 + refine prompts（plan 带当前页身份段）
        - compile: 全量 index + 存 source 页 + compile prompts
        显式传入 index_reader/save_sources 时覆盖模式默认。

        Args:
            llm: LLM 客户端。
            vlm: VLM 客户端（图片 caption）。
            wiki_dir: wiki 根目录。
            source_records_dir: 工作区中的来源摘要存档目录。
            chunk_size: 分块大小（字符）。
            model_context: extract 阶段的模型上下文窗口。
            extract_concurrency: extract 并发数。
            mode: "compile" 或 "refine"。
            index_reader: 自定义 index 读取钩子。
            save_sources: 是否写 sources 页（覆盖模式默认）。
            on_progress: 阶段切换回调，接收稳定的英文阶段代码。
        """
        self._wiki_dir = Path(wiki_dir)
        self._source_records_dir = (
            Path(source_records_dir) if source_records_dir is not None else None
        )
        self._on_progress = on_progress
        if mode not in ("compile", "refine"):
            raise ValueError(f"未知模式: {mode!r}——compile / refine")
        self._mode = mode
        budget = compile_config or CompileConfig()
        chunk_size = budget.chunk_size if chunk_size is None else chunk_size
        model_context = budget.context_window if model_context is None else model_context
        extract_concurrency = (
            budget.extract_concurrency if extract_concurrency is None else extract_concurrency
        )

        from wiki_agent.compiler import prompts as prompts_pkg
        from wiki_agent.compiler.workflows.refine import build_index_excluding_self

        self._prompts = prompts_pkg.refine if mode == "refine" else prompts_pkg.compile
        # index 视图: 显式钩子 > refine 默认排除自身 > 全量（None=默认读法）
        if index_reader is not None:
            self._index_reader = index_reader
        elif mode == "refine":
            self._index_reader = build_index_excluding_self(self._wiki_dir)
        else:
            self._index_reader = None
        # source 页: 显式开关 > refine 默认不存 > compile 默认存
        self._save_sources = save_sources if save_sources is not None else (mode != "refine")

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
            save_source_page=self._save_sources,
            prompts=self._prompts,
            system_tokens=budget.extract_system_tokens,
            output_tokens=budget.extract_output_tokens,
            safety_buffer=budget.context_safety_buffer,
        )
        # 四阶段按模式组装——工厂是唯一知道"模式 = 哪套组合"的地方
        if mode == "refine":
            self._integrator = refine_integrator(llm, wiki_dir=self._wiki_dir)
        else:
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
        async with span("ingest_file", file=raw_file.name, mode=self._mode):
            return await self._ingest_one(raw_file)

    async def _ingest_one(self, raw_file: RawFileProperties) -> IngestOutcome:
        outcome = IngestOutcome(source=raw_file.name)

        # 1. Convert
        self._notify_progress(IngestStage.CONVERT)
        try:
            cf = await self._converter.convert(raw_file)
        except IngestError:
            raise
        except WikiAgentError as e:
            raise IngestError(IngestStage.CONVERT, str(e), source=raw_file.name, cause=e) from e
        except Exception as e:
            raise IngestError(
                IngestStage.CONVERT, f"未分类: {e}", source=raw_file.name, cause=e
            ) from e
        outcome.converted_chars = len(cf.content)
        logger.info("  [%s] → %d chars, %d 图片", cf.ext, len(cf.content), cf.content.count("!["))

        # 转换为空不是“没有 chunk”，而是 source 没有进入编译链。
        # 必须在 LLM 前失败，禁止空内容走全文兜底后产生标题驱动的页面。
        if not cf.content.strip():
            raise IngestError(
                IngestStage.CONVERT,
                "转换后内容为空，禁止调用 LLM。",
                source=raw_file.name,
                error_code="empty_converted_content",
            )

        # 2. Chunk（空 chunk 用全文兜底——单 chunk 模拟对象）
        ck_list = self._chunker.chunk(cf)
        if not ck_list:
            logger.warning("  Chunk 为空，用全文兜底")
            ck_list = [_FallbackChunk(content=cf.content)]

        # 3. Extract
        self._notify_progress(IngestStage.EXTRACT)
        sd = SourceDocument(
            name=cf.name,
            ext=cf.ext,
            path=str(cf.path),
            chunks=[_to_source_chunk(ck, len(ck_list), cf.name) for ck in ck_list],
        )
        try:
            outcome.extract = await self._extractor.extract(sd)
            logger.info("  摘要: %d chars", len(outcome.extract.document_summary))
        except IngestError:
            raise
        except WikiAgentError as e:
            raise IngestError(IngestStage.EXTRACT, str(e), source=raw_file.name, cause=e) from e
        except Exception as e:
            raise IngestError(
                IngestStage.EXTRACT, f"未分类: {e}", source=raw_file.name, cause=e
            ) from e

        # 非空 source 的空摘要属于抽取失败；低信息但有事实的摘要仍可
        # 继续到 plan，由 planner 决定是否 no-op。
        if not outcome.extract.document_summary.strip():
            raise IngestError(
                IngestStage.EXTRACT,
                "Extractor 返回空摘要，禁止进入 plan/execute。",
                source=raw_file.name,
                error_code="empty_extract_summary",
            )

        # 4. Search → Analyze → Plan（analyze/plan 内部已 raise IngestError）
        # index 视图: 有钩子用钩子（refine 排除自身），否则默认读全量。
        # 首跑/被删时显式初始化——存在性保证在入口做一次，
        # 后续环节读到的要么是真实 index 要么是刚建的空 index。
        self._ensure_index()
        if self._index_reader is not None:
            index_content = self._index_reader(raw_file.path)
        else:
            index_content = (self._wiki_dir / "index.md").read_text(encoding="utf-8")
        schema = self._read_optional("schema.md")
        purpose = self._read_optional("purpose.md")

        # 契约: ingest_one 只抛 IngestError——search/analyze/plan 的
        # 未预期异常（retry 的 RuntimeError、代码 bug）在此包装，
        # 边界只需一个 except 就能完整收集。IngestError 原样透传（保 stage）。
        self._notify_progress(IngestStage.SEARCH)
        try:
            search_result = await self._integrator.search(outcome.extract, index_content)
        except IngestError:
            raise
        except Exception as e:
            raise IngestError(
                IngestStage.SEARCH, f"未分类: {e}", source=raw_file.name, cause=e
            ) from e
        logger.info("  search: %d 个候选", len(search_result.rel_paths))
        outcome.search = search_result
        self._notify_progress(IngestStage.ANALYZE)
        try:
            outcome.analysis = await self._integrator.analyze(outcome.extract, search_result)
        except IngestError:
            raise
        except Exception as e:
            raise IngestError(
                IngestStage.ANALYZE, f"未分类: {e}", source=raw_file.name, cause=e
            ) from e
        self._notify_progress(IngestStage.PLAN)
        try:
            # planner 自己知道要不要 current_page（needs_current_page）——
            # pipeline 无条件传，模式知识不泄漏到这里
            outcome.plan = await self._integrator.plan(
                outcome.extract,
                outcome.analysis,
                schema=schema,
                purpose=purpose,
                index_content=index_content,
                current_page=self._current_page(raw_file),
            )
        except IngestError:
            raise
        except Exception as e:
            raise IngestError(
                IngestStage.PLAN, f"未分类: {e}", source=raw_file.name, cause=e
            ) from e

        # 5. Execute + index 更新（execute 失败隔离在页级，这里失败是批级问题）
        n = len(outcome.plan.page_targets)
        if n == 0:
            outcome.noop = True
            logger.info("  plan: 无页面操作")
            return outcome
        self._notify_progress(IngestStage.EXECUTE)
        try:
            outcome.pages_written = [
                t.wiki_path
                for t in await self._integrator.execute(outcome.plan, outcome.extract)
                if (self._wiki_dir / _normalize(t.wiki_path)).exists()
            ]
        except IngestError:
            # execute 部分成功：失败 source 的存活页面仍要进 index。
            # 若跳过，磁盘有 index 无的页面（幽灵页）对 search/analyze
            # 不可见，后续编译无法命中——历史缺陷：部分失败的 source
            # 全部页面漏索引，重试补页也修不回（index 只追加不重建）。
            written = [
                t.wiki_path
                for t in outcome.plan.page_targets
                if (self._wiki_dir / _normalize(t.wiki_path)).exists()
            ]
            if written:
                self._append_index(outcome.plan, written)
            raise
        except WikiAgentError as e:
            raise IngestError(IngestStage.EXECUTE, str(e), source=raw_file.name, cause=e) from e
        except Exception as e:
            raise IngestError(
                IngestStage.EXECUTE, f"未分类: {e}", source=raw_file.name, cause=e
            ) from e

        self._append_index(outcome.plan, outcome.pages_written)
        return outcome

    # 内部

    def _notify_progress(self, stage: IngestStage) -> None:
        callback = getattr(self, "_on_progress", None)
        if callback is not None:
            callback(stage.value)

    def _current_page(self, raw_file) -> str:
        """refine 模式的当前页面 slug——compile 模式留空。

        （只更新当前页的过滤与 page_meta 已归 PolisherPlanner——
        planner 自持模式语义，pipeline 只剩 slug 计算这个纯函数。）

        Args:
            raw_file: 源文件属性。

        Returns:
            refine 模式的页面 slug；compile 模式或路径越界返回空串。
        """
        if self._mode != "refine":
            return ""
        try:
            rel = Path(raw_file.path).relative_to(self._wiki_dir)
            return str(rel).replace(".md", "")
        except ValueError:
            return ""

    def _ensure_index(self) -> None:
        """index 存在性保证——首跑/被删时创建空文件。

        显式初始化优于读时吞异常: index 缺失是合法状态（新库），
        读路径保持严格（FileNotFoundError 该炸就炸），
        创建职责在入口这一步完成。
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
        """新页面进 index——跳过生成失败的（幽灵页面防线）。

        index 已由 _ensure_index 保证存在——这里读失败是 bug，不吞。

        Args:
            plan: 集成计划（取 target 的 slug/标题）。
            pages_written: 实际落盘的页面路径列表。
        """
        index_path = self._wiki_dir / "index.md"
        existing = index_path.read_text(encoding="utf-8")
        # pages_written 带 .md 后缀（normalize 后），与 slug 比对前先归一
        written_slugs = {p.replace(".md", "") for p in pages_written}
        fresh: list[str] = []
        for pt in plan.page_targets:
            slug = pt.wiki_path.replace("wiki/", "").replace(".md", "")
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
            fresh.append(
                f"- [[{slug}]] — [{page_type}] {pt.wiki_path} — {title}"
                f"{' — ' + summary if summary else ''}"
                f"{' — goal: ' + goal if goal else ''}"
            )
        if fresh:
            index_path.write_text(
                existing.rstrip() + "\n" + "\n".join(fresh) + "\n",
                encoding="utf-8",
            )
            logger.info("  index: +%d 条目", len(fresh))


# 工具


class _FallbackChunk:
    """chunk 全空时的兜底——单 chunk = 全文。"""

    def __init__(self, content: str):
        self.content = content
        self.chunk_index = 0


def _to_source_chunk(ck, total: int, source_name: str) -> SourceChunk:
    """ingestion chunk → compiler 模型——标题路径从 chunker metadata 取。

    heading 的唯一权威在 chunker（切分时已知 chunk 归属哪个 section），
    消费端不重新解析——源头记录、下游取用，解析猜错的问题不存在。
    source_name 由调用方传入：chunk 级摘要 prompt 需要出处标注。

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
