"""Phase 1 — 从结构化源文档生成纯文档摘要（不做实体提取）。

两种摘要策略:
  - 均匀分配: chunk 少、上下文窗口大时，每个 chunk 独立并行摘要
  - 滚动压缩: chunk 多时，逐 chunk 累积全局摘要
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from wiki_agent.compiler.models import (
    NO_THINKING,
    ChunkSummary,
    ExtractResult,
    SourceChunk,
    SourceDocument,
    SourcePage,
)
from wiki_agent.compiler.prompts import compile as _compile_prompts
from wiki_agent.conversation import Message
from wiki_agent.llm.llm import LLMClient
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import get_logger

logger = get_logger("EXTRACTOR")

# 默认 token 分配
_SYSTEM_TOKENS = 4_000
_OUTPUT_TOKENS = 6_000
_PER_CHUNK_MIN = 500  # 均匀分配时单个 chunk 最低 token
_DEFAULT_MODEL_CONTEXT = 128_000


class Extractor:
    """SourceDocument → ExtractResult。"""

    def __init__(
        self,
        llm: LLMClient,
        *,
        model_context: int = _DEFAULT_MODEL_CONTEXT,
        max_concurrency: int = 5,
        source_records_dir: str | Path | None = None,
        save_source_page: bool = True,
        prompts=_compile_prompts,
        system_tokens: int = _SYSTEM_TOKENS,
        output_tokens: int = _OUTPUT_TOKENS,
        safety_buffer: int = 0,
    ):
        self._llm = llm
        self._model_context = model_context
        self._system_tokens = system_tokens
        self._output_tokens = output_tokens
        self._safety_buffer = safety_buffer
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._source_records_dir = Path(source_records_dir) if source_records_dir else None
        # compile/sync 存档源摘要页；refine 输入就是 wiki 页面，再存 = 自我复制
        self._save_sources = save_source_page
        # prompt 模块（compile/refine）——模式差异由 pipeline 注入
        self._prompts = prompts

    # 公开 API

    async def extract(self, source: SourceDocument) -> ExtractResult:
        """从 ``SourceDocument`` 提取知识。

        自动选择均匀分配或滚动压缩策略。
        save_sources 时构造档案页内容放进 ``result.source_page``——
        只构造不落盘，写盘由成功结算方（sync outcome / compile 批）执行。

        Args:
            source: 结构化源文档（chunks + 元信息）。

        Returns:
            ExtractResult；无 chunk 时返回空摘要结果。
        """
        chunks = source.chunks
        if not chunks:
            return ExtractResult(source_identity=source.name)

        budget = self._summary_budget()
        uniform = (budget // len(chunks)) >= _PER_CHUNK_MIN

        if uniform:
            logger.info(
                "[%s] 均匀分配, %d chunks, ~%d tokens/chunk",
                source.name,
                len(chunks),
                budget // len(chunks),
            )
            summaries = await self._extract_uniform(chunks, budget)
        else:
            logger.info(
                "[%s] 滚动压缩, %d chunks",
                source.name,
                len(chunks),
            )
            summaries = await self._extract_rolling(chunks, budget)

        logger.info("  Extract 摘要完成: %d chunks → 开始合成", len(summaries))
        return await self._synthesize(source, summaries)

    # 均匀分配

    async def _extract_uniform(
        self,
        chunks: list[SourceChunk],
        total_budget: int,
    ) -> list[ChunkSummary]:
        per_chunk = max(_PER_CHUNK_MIN, total_budget // len(chunks))

        async def summarize(chunk: SourceChunk) -> ChunkSummary:
            async with self._semaphore:
                result = ChunkSummary(
                    chunk_index=chunk.index,
                    heading_path=chunk.heading_path,
                    text=await self._summarize_chunk(chunk, per_chunk),
                )
                logger.debug("    chunk %d/%d 完成", chunk.index + 1, len(chunks))
                return result

        return await asyncio.gather(*[summarize(c) for c in chunks])

    # 滚动压缩

    async def _extract_rolling(
        self,
        chunks: list[SourceChunk],
        budget: int,
    ) -> list[ChunkSummary]:
        digest = ""
        summaries: list[ChunkSummary] = []

        for chunk in chunks:
            response = await async_invoke_with_retry(
                self._llm,
                [
                    Message(role="system", content=self._prompts.rolling_system()),
                    Message(
                        role="user",
                        content=self._prompts.rolling_user(
                            chunk,
                            digest,
                            len(chunks),
                        ),
                    ),
                ],
                # digest 输出受控——目标长度的 2 倍留重写缓冲，防膨胀
                max_tokens=min(budget, self._prompts.DIGEST_TARGET_TOKENS * 2),
                extra_body=NO_THINKING,
            )
            digest = response.content
            summaries.append(
                ChunkSummary(
                    chunk_index=chunk.index,
                    heading_path=chunk.heading_path,
                    text=digest,
                )
            )

        return summaries

    # 合成

    async def _synthesize(
        self,
        source: SourceDocument,
        summaries: list[ChunkSummary],
    ) -> ExtractResult:
        """chunk 摘要 → 纯文档级概述。

        Args:
            source: 源文档（元信息进 prompt）。
            summaries: 各 chunk 摘要列表。

        Returns:
            汇总结果（并保存 source 页）。
        """
        parts: list[str] = [
            f"# 源文件: {source.name}",
            f"路径: {source.path}",
            f"共 {source.chunk_count} 个片段",
            "",
        ]
        for s in summaries:
            head = f"（{s.heading_path}）" if s.heading_path else ""
            parts.append(f"## Chunk {s.chunk_index} {head}\n{s.text}")
            parts.append("")

        joined = "\n".join(parts)

        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=self._prompts.synthesis_prompt()),
                Message(role="user", content=joined),
            ],
            max_tokens=_OUTPUT_TOKENS,
            extra_body=NO_THINKING,
        )
        raw = response.content
        result = ExtractResult(
            source_identity=source.name,
            document_summary=raw,
        )
        if self._save_sources and self._source_records_dir is not None:
            page = self._build_source_page(source, result)
            if page is not None:
                result.source_page = page
        return result

    def _build_source_page(self, source: SourceDocument, result: ExtractResult) -> SourcePage | None:
        """构造源文档档案页（代码维护，不依赖 LLM plan 阶段）。

        只构造内容不落盘——档案页属于结算面：失败/取消的执行不留下它，
        成功的 job 在终态联动时写入。摘要为空时返回 None——避免 LLM
        空响应生成空白 source 页。

        Args:
            source: 源文档。
            result: 摘要结果。
        """
        raw_summary = result.document_summary.strip()
        if not raw_summary:
            logger.warning("  ✗ 文档摘要为空，跳过 source 页: %s", source.name)
            return None

        slug = self._slugify_source(source.name)

        from datetime import date as _date

        today = _date.today().isoformat()

        display_name = source.name.rsplit(".", 1)[0] if "." in source.name else source.name
        summary_line = raw_summary.split("\n")[0].strip().lstrip("# ")[:80]

        frontmatter = "\n".join(
            [
                "---",
                "type: source",
                f'title: "{display_name}"',
                f'summary: "{summary_line}"',
                # 档案页统一静态 goal——scan 的 goal 必填判定对全页面一致，
                # 档案页使命就是"溯源"（知识页 goal 由 LLM 写，档案页代码写）
                'goal: "源文件档案——保存本文档的提取摘要供溯源"',
                # 档案页无交叉引用是常态——显式空数组对齐 normalize 定稿链格式
                "related: []",
                f"created: {today}",
                f"updated: {today}",
                f'sources: ["{source.name}"]',
                "---",
            ]
        )
        content = f"{frontmatter}\n# {display_name}\n\n{result.document_summary}"
        logger.info("  source page constructed: %s (%d chars)", slug, len(content))
        return SourcePage(slug=slug, content=content.strip() + "\n")

    # 单个 chunk 摘要

    async def _summarize_chunk(self, chunk: SourceChunk, max_tokens: int) -> str:
        """单个 chunk 的摘要（均匀分配模式）。

        prompt 包含 chunk 在源文件中的位置信息（序号、标题路径），
        帮助 LLM 理解上下文。

        Args:
            chunk: 待摘要 chunk。
            max_tokens: 摘要生成上限。

        Returns:
            摘要文本。
        """
        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=self._prompts.chunk_system()),
                Message(role="user", content=self._prompts.chunk_user(chunk)),
            ],
            max_tokens=max_tokens,
            extra_body=NO_THINKING,
        )
        return response.content

    # 工具

    @staticmethod
    def _slugify_source(filename: str) -> str:
        """文件名 → kebab-case slug（去扩展名）。

        Args:
            filename: 源文件名。

        Returns:
            slug（空名兜底 "untitled"）。
        """
        name = filename.rsplit(".", 1)[0] if "." in filename else filename
        slug = name.lower().strip()
        slug = re.sub(r"[^a-z0-9一-鿿]+", "-", slug)
        return slug.strip("-") or "untitled"

    def _summary_budget(self) -> int:
        available = (
            self._model_context - self._system_tokens - self._output_tokens - self._safety_buffer
        )
        # context_window 是请求可用的输入窗口，不应直接变成单次输出的
        # max_tokens；输出上限由 extract_output_tokens 控制，避免向 provider
        # 请求几十万 tokens。
        return min(self._output_tokens, max(2_000, available))


def write_source_page(source_records_dir: Path, page: SourcePage) -> Path:
    """结算方落盘档案页——sync outcome / compile 批共用的唯一写入口。"""
    source_records_dir.mkdir(parents=True, exist_ok=True)
    target = source_records_dir / f"{page.slug}.md"
    target.write_text(page.content, encoding="utf-8")
    return target
