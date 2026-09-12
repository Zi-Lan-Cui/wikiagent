"""Analyze 阶段——文档与候选页的两段式关系分析（模式间零差异）。"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.integration.checks import _check_analyze_json
from wiki_agent.compiler.integration.parse import _extract_headings, _parse_analysis
from wiki_agent.compiler.models import _NO_THINKING, AnalysisResult, ExtractResult, SearchResult
from wiki_agent.conversation import Message
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.llm.llm import LLMClient
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import get_logger
from wiki_agent.wiki.frontmatter import split_frontmatter

logger = get_logger("STAGES")

_ANALYZE_TOKENS = 6_000


class Analyzer:
    """analyze 阶段: 文档与候选页的两段式关系分析。模式间零差异。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def _read_page(self, wiki_path: str) -> str:
        """读 wiki 页面全文。

        Args:
            wiki_path: 页面相对路径。

        Returns:
            页面内容；页面不存在返回空串。
        """
        try:
            return (self._wiki_dir / wiki_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def analyze(
        self,
        extract: ExtractResult,
        result: SearchResult,
    ) -> AnalysisResult:
        """关系分析——新文档与 search 候选页面的两段式分析。

        Args:
            extract: 源文档抽取结果。
            result: search 阶段输出的候选页面。

        Returns:
            分析结果（实体/概念/关系 + 自由分析文本）。

        Raises:
            IngestError: 校验穷尽后仍失败。
        """
        # 空候选也走完整分析——候选页面只影响 relationship 段，不影响
        # 文档内部知识结构的自由分析（entities/concepts/呼应/对比）。
        # 早退会让 plan 失去决策依据，且使首跑结果依赖文件顺序。
        outlines: list[str] = []
        for path in result.rel_paths:
            content = await self._read_page(path)
            fm = split_frontmatter(content)[0] if content else {}
            title = fm.get("title", "")
            summary = fm.get("summary", "")
            page_type = fm.get("type", "")
            sources = fm.get("sources", "")
            related = fm.get("related", "")
            gaps = fm.get("gaps", "")
            goal = fm.get("goal", "")
            headings = _extract_headings(content)
            slug = path.replace("wiki/", "").replace(".md", "")
            meta = f"- [[{slug}]] — [{page_type}] {path} — {title}"
            if summary:
                meta += f" — {summary}"
            if goal:
                meta += f" — 使命: {goal}"
            if gaps:
                meta += f" — 缺口声明: {gaps}"
            if sources:
                meta += f" — 来源: {sources}"
            if related:
                meta += f" — 已有引用: {related}"
            if headings:
                meta += f"\n{headings}"
            outlines.append(meta)

        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=self._prompts.analyze_system()),
                Message(
                    role="user",
                    content=self._prompts.analyze_user(
                        extract,
                        "\n\n".join(outlines),
                    ),
                ),
            ],
            max_tokens=_ANALYZE_TOKENS,
            check=lambda content: _check_analyze_json(
                content,
                candidates=result.rel_paths,
                # 当前文档真实 slug 并入合法集——LLM 用真实路径自称
                # 比固定标识 current-doc 自然（实测 refine 高频违规）
                extra_refs={extract.source_identity},
            ),
            extra_body=_NO_THINKING,
            max_retries=2,
        )
        raw = response.content
        # 空响应/校验不过由 retry 层处理——这里只做穷尽后的显式报告。
        if not response.check_ok:
            raise IngestError(
                IngestStage.ANALYZE,
                f"analyze 输出校验失败（重试后仍失败）: {response.check_reason}",
                source=extract.source_identity,
                raw=raw,
                error_code="output_validation",
                error_class="transient",
                retry_policy="auto_retry",
            )
        analysis = _parse_analysis(raw, extract.source_identity)
        logger.info(
            "  analysis: %d entities, %d concepts, %d relationships, 自由分析 %d chars",
            len(analysis.entities),
            len(analysis.concepts),
            len(analysis.relationships),
            len(analysis.analysis_text),
        )
        return analysis
