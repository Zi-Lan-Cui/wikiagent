"""Compiler — LLM 知识编译器。

Phase 1 (提取):   结构化分块 → 文档摘要
Phase 2 (集成):   摘要 + index → 受影响页面 → 异步更新
                  （search / analyze / plan / execute 四阶段）

集成四阶段由策展人决策（新建/更新开放）。

子包按依赖方向分层：数据模型 → 页面规则（无 LLM）→ 提取 →
集成 → 工作流编排。
"""

from wiki_agent.compiler.extraction import Extractor
from wiki_agent.compiler.integration import Integrator, compile_integrator
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
