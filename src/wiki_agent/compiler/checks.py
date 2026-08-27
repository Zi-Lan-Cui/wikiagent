"""兼容导入：校验层已按职责拆分。

页面级检测原子与闸门 → :mod:`wiki_agent.compiler.wiki.rules`（无 LLM 底层）
LLM 阶段输出结构校验 → :mod:`wiki_agent.compiler.integration.checks`

迁移期保留本 facade，零新代码引用后即删除。
"""

from wiki_agent.compiler.integration.checks import (
    _VALID_DISPOSITIONS,
    _VALID_IMPORTANCE,
    _VALID_RELATIONS,
    _check_analyze_json,
    _check_json_array,
    _check_plan_json,
)
from wiki_agent.compiler.wiki.rules import (
    _FENCE_OPEN,
    _WIKILINK_RE,
    _body_without_title,
    _check_page_body,
    _check_page_frontmatter,
    _check_page_output,
    _check_wikilink_has_text,
    _extract_body,
    _iter_code_runs,
    count_unclosed_fences,
    iter_text_outside_code,
)

__all__ = [
    "_VALID_RELATIONS",
    "_VALID_IMPORTANCE",
    "_VALID_DISPOSITIONS",
    "_check_analyze_json",
    "_check_plan_json",
    "_check_json_array",
    "_FENCE_OPEN",
    "_iter_code_runs",
    "iter_text_outside_code",
    "count_unclosed_fences",
    "_extract_body",
    "_body_without_title",
    "_WIKILINK_RE",
    "_check_page_body",
    "_check_page_output",
    "_check_page_frontmatter",
    "_check_wikilink_has_text",
]
