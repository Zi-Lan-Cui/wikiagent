"""兼容导入面——四阶段实现已拆至 search/analyze/plan/execute + common。

真实模块:
    :mod:`~wiki_agent.compiler.integration.search`   Searcher
    :mod:`~wiki_agent.compiler.integration.analyze`  Analyzer
    :mod:`~wiki_agent.compiler.integration.plan`     Planner/Curator/Polisher + 领域工具
    :mod:`~wiki_agent.compiler.integration.execute`  Executor
    :mod:`~wiki_agent.compiler.integration.common`   load_valid_slugs/extract_slugs_from_index
    :mod:`~wiki_agent.compiler.integration.workflow` Integrator + 组装工厂

本模块迁移期仅作 re-export，零新代码引用后即删除。
"""

from __future__ import annotations

from wiki_agent.compiler.integration.analyze import _ANALYZE_TOKENS, Analyzer
from wiki_agent.compiler.integration.common import (
    extract_slugs_from_index,
    load_valid_slugs,
)
from wiki_agent.compiler.integration.execute import (
    _PAGE_GEN_RETRIES,
    _UPDATE_TOKENS,
    Executor,
)
from wiki_agent.compiler.integration.plan import (
    _PLAN_TOKENS,
    CuratorPlanner,
    Planner,
    PolisherPlanner,
    _format_analysis_for_plan,
    filter_plan_refs,
)
from wiki_agent.compiler.integration.search import (
    _SEARCH_PAGE_DIRS,
    _SEARCH_TOKENS,
    Searcher,
    _filter_search_paths,
)

__all__ = [
    "Searcher",
    "Analyzer",
    "Planner",
    "CuratorPlanner",
    "PolisherPlanner",
    "Executor",
    "load_valid_slugs",
    "extract_slugs_from_index",
    "filter_plan_refs",
    "_format_analysis_for_plan",
    "_filter_search_paths",
    "_SEARCH_PAGE_DIRS",
    "_SEARCH_TOKENS",
    "_ANALYZE_TOKENS",
    "_PLAN_TOKENS",
    "_UPDATE_TOKENS",
    "_PAGE_GEN_RETRIES",
]
