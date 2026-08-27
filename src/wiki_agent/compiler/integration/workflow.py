"""Integrator——四阶段组装器（facade）+ 组装工厂。

与四个阶段模块的分层: search/analyze/plan/execute 是"执行者"
（每个阶段怎么做），本模块是"组装"（哪四个执行者配成一条链）。

模式知识只在这里: compile_integrator() = 策展人链，
refine_integrator() = 润色师链。新模式的加入 = 新工厂函数。
"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.integration.analyze import Analyzer
from wiki_agent.compiler.integration.execute import Executor
from wiki_agent.compiler.integration.plan import (
    CuratorPlanner,
    Planner,
    PolisherPlanner,
)
from wiki_agent.compiler.integration.search import Searcher
from wiki_agent.compiler.models import (
    AnalysisResult,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
    SearchResult,
)
from wiki_agent.compiler.prompts import compile as compile_prompts
from wiki_agent.compiler.prompts import refine as refine_prompts
from wiki_agent.llm.llm import LLMClient


class Integrator:
    """四阶段组装器——保持 pipeline 的编排形状不变。

    模式差异收敛在"组装哪个 Planner"（工厂函数），facade 只转发。
    """

    def __init__(
        self, searcher: Searcher, analyzer: Analyzer, planner: Planner, executor: Executor
    ):
        self._searcher = searcher
        self._analyzer = analyzer
        self._planner = planner
        self._executor = executor

    async def search(self, extract: ExtractResult, index_content: str) -> SearchResult:
        return await self._searcher.search(extract, index_content)

    async def analyze(self, extract: ExtractResult, result: SearchResult) -> AnalysisResult:
        return await self._analyzer.analyze(extract, result)

    async def plan(
        self,
        extract: ExtractResult,
        analysis: AnalysisResult,
        *,
        schema: str = "",
        purpose: str = "",
        index_content: str = "",
        current_page: str = "",
    ) -> IntegrationPlan:
        """转发到 planner——current_page 只传给需要它的（refine 润色师）。

        共享签名保持 current_page 参数（pipeline 无条件传），但
        模式知识在 Planner.needs_current_page——CuratorPlanner 永远
        收不到这个参数，接口不再被模式差异污染。

        Args:
            extract: 源文档抽取结果。
            analysis: analyze 阶段的分析结果。
            schema: 目录规范文本。
            purpose: 知识库使命文本。
            index_content: index.md 全文。
            current_page: 当前页面 slug（仅 needs_current_page 的
                planner 接收）。

        Returns:
            集成计划。
        """
        if isinstance(self._planner, PolisherPlanner):
            return await self._planner.plan(
                extract,
                analysis,
                schema=schema,
                purpose=purpose,
                index_content=index_content,
                current_page=current_page,
            )
        return await self._planner.plan(
            extract,
            analysis,
            schema=schema,
            purpose=purpose,
            index_content=index_content,
        )

    async def execute(self, plan: IntegrationPlan, extract: ExtractResult) -> list[PageTarget]:
        return await self._executor.execute(plan, extract)


# ════════════════════════════════════════════════════════════
#  组装工厂——唯一知道"模式 = 哪套组合"的地方
# ════════════════════════════════════════════════════════════


def compile_integrator(llm: LLMClient, *, wiki_dir: str | Path) -> Integrator:
    """组装 compile 模式——策展人决策（new/update 开放）。

    Args:
        llm: LLM 客户端。
        wiki_dir: wiki 根目录。

    Returns:
        策展人四阶段链。
    """
    p = compile_prompts
    return Integrator(
        Searcher(llm, wiki_dir, p),
        Analyzer(llm, wiki_dir, p),
        CuratorPlanner(llm, wiki_dir, p),
        Executor(llm, wiki_dir, p),
    )


def refine_integrator(llm: LLMClient, *, wiki_dir: str | Path) -> Integrator:
    """组装 refine 模式——润色师决策（只更新自己，读本页 goal/gaps 判断完成度）。

    Args:
        llm: LLM 客户端。
        wiki_dir: wiki 根目录。

    Returns:
        润色师四阶段链。
    """
    p = refine_prompts
    return Integrator(
        Searcher(llm, wiki_dir, p),
        Analyzer(llm, wiki_dir, p),
        PolisherPlanner(llm, wiki_dir, p),
        Executor(llm, wiki_dir, p),
    )
