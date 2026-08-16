"""Compiler — LLM 知识编译器。

Phase 1 (Extractor):  chunk → 摘要 → ExtractResult
Phase 2 (Integrator): ExtractResult + index.md → 受影响页面 → 异步更新
    四阶段拆分为独立类（stages.py），组装器与工厂独立（integrator.py）:
    compile_integrator() = Searcher + Analyzer + CuratorPlanner + Executor
    refine_integrator()  = Searcher + Analyzer + PolisherPlanner + Executor
"""

from wiki_agent.compiler.models import (
    ChunkSummary,
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
    SearchResult,
    SourceChunk,
    SourceDocument,
)
from wiki_agent.compiler.extract import Extractor
from wiki_agent.compiler.integrator import (
    Integrator,
    compile_integrator,
    refine_integrator,
)
