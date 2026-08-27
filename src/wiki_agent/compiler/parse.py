"""兼容导入：解析层已按职责拆分。

frontmatter 解析 → :mod:`wiki_agent.compiler.wiki.frontmatter`（无 LLM 底层）
LLM 原始输出 → 模型 → :mod:`wiki_agent.compiler.integration.parse`

迁移期保留本 facade，零新代码引用后即删除。
"""

from wiki_agent.compiler.integration.parse import (
    _clean_title,
    _extract_analyze_parts,
    _extract_headings,
    _normalize_wiki_path,
    _parse_analysis,
    _parse_plan,
    _parse_references,
    _parse_search_result,
    _strip_fence,
    _try_repair_trailing_braces,
)
from wiki_agent.compiler.wiki.frontmatter import parse_frontmatter, split_frontmatter

__all__ = [
    "split_frontmatter",
    "parse_frontmatter",
    "_strip_fence",
    "_try_repair_trailing_braces",
    "_parse_search_result",
    "_parse_analysis",
    "_parse_plan",
    "_parse_references",
    "_extract_headings",
    "_extract_analyze_parts",
    "_clean_title",
    "_normalize_wiki_path",
]
