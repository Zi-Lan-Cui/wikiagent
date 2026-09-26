"""Integrator：四阶段组装器与组装工厂。

阶段模块实现每个阶段的具体做法；本模块把四个阶段组合成执行链。
"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.integration.analyze import Analyzer
from wiki_agent.compiler.integration.execute import Executor
from wiki_agent.compiler.integration.plan import CuratorPlanner, Planner
from wiki_agent.compiler.integration.search import Searcher
from wiki_agent.compiler.models import (
    AnalysisResult,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
    SearchResult,
)
from wiki_agent.compiler.prompts import compile as compile_prompts
from wiki_agent.llm.llm import LLMClient


class Integrator:
    """四阶段组装器——pipeline 的编排形状，本类只转发。"""

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
    ) -> IntegrationPlan:
        """转发给 planner。

        Args:
            extract: 源文档抽取结果。
            analysis: analyze 阶段的分析结果。
            schema: 目录规范文本。
            purpose: 知识库使命文本。
            index_content: index.md 全文。

        Returns:
            集成计划。
        """
        return await self._planner.plan(
            extract,
            analysis,
            schema=schema,
            purpose=purpose,
            index_content=index_content,
        )

    async def execute(self, plan: IntegrationPlan, extract: ExtractResult) -> list[PageTarget]:
        return await self._executor.execute(plan, extract)


def compile_integrator(llm: LLMClient, *, wiki_dir: str | Path) -> Integrator:
    """组装策展人四阶段链（new/update 开放决策）。

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
