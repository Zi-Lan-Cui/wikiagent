"""Analyze 阶段——文档与候选页的两段式关系分析（模式间零差异）。"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.checked_call import invoke_checked
from wiki_agent.compiler.integration.checks import check_analyze_json
from wiki_agent.compiler.integration.parse import parse_analysis
from wiki_agent.compiler.models import NO_THINKING, AnalysisResult, ExtractResult, SearchResult
from wiki_agent.conversation import Message
from wiki_agent.errors import IngestStage
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import get_logger
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.sections import TOP_LABEL, pick_gist_limit, text_sections

logger = get_logger("STAGES")

_ANALYZE_TOKENS = 6_000

# 候选页节摘要预算：与路由大纲同形状、独立阈值——候选是 5~8 页的
# 节数合计，12 节上限意味着多候选时自动落到短档。
ANALYZE_GIST_CHARS = 160
ANALYZE_GIST_SHORT_CHARS = 40
ANALYZE_MAX_GIST_SECTIONS = 12


def render_candidates(parsed: list[tuple[str, dict, list]]) -> list[str]:
    """候选页大纲行——frontmatter 概览 + 按节摘要（节数合计定档）。

    parsed 项为 (path, frontmatter, sections)。节摘要与路由大纲共用
    wiki.sections 的提取逻辑；候选多时整体落短档，控制提示规模。
    """
    total_secs = sum(len(secs) for _, _, secs in parsed)
    gist_limit = pick_gist_limit(
        total_secs, ANALYZE_GIST_CHARS, ANALYZE_GIST_SHORT_CHARS, ANALYZE_MAX_GIST_SECTIONS
    )
    outlines: list[str] = []
    for path, fm, secs in parsed:
        title = fm.get("title", "")
        summary = fm.get("summary", "")
        page_type = fm.get("type", "")
        sources = fm.get("sources", "")
        related = fm.get("related", "")
        gaps = fm.get("gaps", "")
        goal = fm.get("goal", "")
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
        if secs:
            section_lines = "\n".join(
                f"  - {s.heading or TOP_LABEL} —— {s.gist(gist_limit)}" for s in secs
            )
            meta += f"\n{section_lines}"
        outlines.append(meta)
    return outlines


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
        parsed: list[tuple[str, dict, list]] = []
        for path in result.rel_paths:
            content = await self._read_page(path)
            fm = split_frontmatter(content)[0] if content else {}
            slug = path.replace("wiki/", "").replace(".md", "")
            parsed.append((path, fm, text_sections(content, slug) if content else []))
        outlines = render_candidates(parsed)

        # 校验穷尽由 invoke_checked 显式报告（空响应由 retry 层处理）
        response = await invoke_checked(
            self._llm,
            stage=IngestStage.ANALYZE,
            action="analyze",
            source=extract.source_identity,
            messages=[
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
            check=lambda content: check_analyze_json(
                content,
                candidates=result.rel_paths,
                # 当前文档真实 slug 并入合法集——LLM 用真实路径自称
                # 比固定标识 current-doc 自然（实测高频违规点）
                extra_refs={extract.source_identity},
            ),
            extra_body=NO_THINKING,
            max_attempts=2,
        )
        analysis = parse_analysis(response.content, extract.source_identity)
        logger.info(
            "  analysis: %d entities, %d concepts, %d relationships, 自由分析 %d chars",
            len(analysis.entities),
            len(analysis.concepts),
            len(analysis.relationships),
            len(analysis.analysis_text),
        )
        return analysis
