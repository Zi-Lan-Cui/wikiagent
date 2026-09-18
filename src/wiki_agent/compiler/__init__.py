"""Compiler — LLM 知识编译器。

Phase 1 (Extractor):  chunk → 摘要 → ExtractResult
Phase 2 (Integrator): ExtractResult + index.md → 受影响页面 → 异步更新
    四阶段独立成模块（integration/search·analyze·plan·execute），组装器与
    工厂在 integration/workflow:
    compile_integrator() = Searcher + Analyzer + CuratorPlanner + Executor
    refine_integrator()  = Searcher + Analyzer + PolisherPlanner + Executor

子包按依赖方向分层（下层绝不 import 上层）:
    models       纯数据（含 _NO_THINKING 共享常量）——最底
    wiki/        frontmatter/rules/normalize/quality——无 LLM 的页面规则
    extraction/  Extractor（Phase 1）
    integration/ 四阶段 + parse/checks（Phase 2）+ workflow 组装
    restructure/     结构重组 proposal/review/resolve/execute/rewrite/transaction
    workflows/   ingest/refine/failures/retry 工作流编排
"""

from wiki_agent.compiler.extraction import Extractor
from wiki_agent.compiler.integration import (
    Integrator,
    compile_integrator,
    refine_integrator,
)
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
