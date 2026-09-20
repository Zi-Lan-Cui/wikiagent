"""refine 模式 prompt——wiki 自编译（只拿不放）。

角色与 compile 完全不同: compile 的 plan 是策展人（对外部文档做
new/update 决策），refine 的 plan 是润色师——页面只更新自己
（补交叉引用、刷新 gaps/summary）。

search/analyze 等阶段的 prompt 复用 compile，plan 完全独立实现——
共享文本只会互相污染。
"""

from __future__ import annotations

from wiki_agent.compiler.models import ExtractResult
from wiki_agent.compiler.prompts.compile import (
    analyze_system,
    analyze_user,
    chunk_system,
    chunk_user,
    new_page_system,
    new_page_user,
    rolling_system,
    rolling_user,
    search_system,
    search_user,
    synthesis_prompt,
    update_system,
    update_user,
)

__all__ = [
    "chunk_system",
    "chunk_user",
    "rolling_system",
    "rolling_user",
    "synthesis_prompt",
    "search_system",
    "search_user",
    "analyze_system",
    "analyze_user",
    "plan_system",
    "plan_user",
    "new_page_system",
    "new_page_user",
    "update_system",
    "update_user",
]

# 模式契约——refine 只允许 update（只拿不放）。
# plan() 用它约束 check_plan_json: LLM 输出 new 直接 retry 修正。
ALLOWED_DISPOSITIONS = {"update"}


def plan_system(
    *,
    schema: str = "",
    purpose: str = "",
) -> str:
    """润色师 prompt 的固定段——角色 + 精炼方向 + 输出格式（跨页面共享）。

    与 compile 的策展人 plan 完全独立:
    - 输入不是外部文档，是 wiki 已有页面（self 已从 index 排除）
    - 输出只有一种合法操作: update 自己
    - 合并/拆分/删除是显式命令的职责，不在 refine 里自动发生

    schema/purpose 是签名兼容位（compile 同名函数使用）——
    refine 的润色师不读它们。

    Args:
        schema: 目录规范（兼容位，不读）。
        purpose: 知识库使命（兼容位，不读）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        p
        for p in [
            "你是知识库的润色师。基于关系分析，对当前页面做精炼。",
            "",
            "## 你的职责（只拿不放）",
            "- 只更新当前页面自己——禁止创建新页、禁止更新其他页面",
            "  （合并/拆分/删除由显式命令处理，不是你的职责）",
            "- 无要改的 → 输出空数组；不要为了让页面看起来更丰富而强行改写",
            "",
            "## 当前页面的精炼方向（按需选择）",
            "0. 目标完成度: 对照 goal（本页使命）判断差距。先确认当前材料是否真的提供了"
            "填补差距所需的事实；只有材料明确支持时才补内容。gaps 已收敛时收紧 gaps，"
            "材料没有提供时保留 gaps，不得凭常识补齐",
            "1. 交叉引用补全: 关系分析指出与候选页的关联，但本页正文/related 缺对应链接 → "
            "update 本页，在合适位置补 [[wikilink]]",
            "2. gaps 刷新: 本页声明了缺口，但候选页显示该缺口已被覆盖 → update 本页，"
            "收敛 gaps 声明",
            "3. summary 修正: 本页内容已演化，summary 过时 → update 本页修正摘要",
            "4. 正文修正: 候选页内容与本页矛盾的 → update 本页标注争议",
            "",
            "## 材料边界与合法 no-op",
            "- 本页已有正文是保留基线；本次关系分析、候选页和 source 摘要只是判断是否需要修改的证据。",
            "- 候选页只用于它明确展示的 goal/gaps/summary/正文事实；不能根据页面名称、领域常识或训练记忆补全。",
            "- 只有以下情况才允许输出 update：新增了有证据支持的事实、确有依据的交叉引用、"
            "可验证的 gaps 收敛、明确冲突标注，或 summary/goal 与现有正文不一致。",
            "- 如果缺口属于材料未提供、超出本页 goal、或当前页面已经完成目标，必须输出空数组。",
            "- 页面变长、措辞更顺、候选页看起来相关，都不是单独修改理由。",
            "- 不得把候选页的内容复制成本页正文；除非关系分析明确证明它是本页缺口的直接证据。",
            "",
            "## 决策依据",
            "关系分析里的 from/to 方向: current-doc 是起点表示'本文档可补充对方'——"
            "此时补的是本页的 [[wikilink]]（指向对方），不是把本页内容写给对方。",
            "",
            "## 输出格式",
            "输出 JSON 对象:",
            '{"page_targets": [{"wiki_path": "wiki/concepts/本页.md", "title": "本页标题",',
            '"disposition": "update", "reason": "具体操作指令",',
            '"references": []}]}',
            "- **references 是对象数组**——每个元素是 "
            '{"slug": "concepts/xxx", "reason": "对照参照"}，'
            '不是字符串数组（["concepts/xxx"] 是错的）。无引用时用 []。',
            '- 无要改的 → {"page_targets": []}',
        ]
        if p
    )


def plan_user(
    extract: ExtractResult,
    analysis_text: str,
    *,
    current_page: str = "",
    page_meta: str = "",
) -> str:
    """润色师决策的动态度——当前页身份/本页 meta/分析/摘要（逐页变化）。

    Args:
        extract: 文档摘要结果。
        analysis_text: 关系分析文本。
        current_page: 当前页面 slug（唯一允许更新的页面）。
        page_meta: 本页 frontmatter 摘要（goal/gaps/summary）——
            目标完成度判断依据。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        p
        for p in [
            f"当前页面: [[{current_page}]]——它就是本次精炼的对象，也是唯一允许更新的页面。",
            "",
            f"## 本页现状\n{page_meta}",
            f"## 关系分析文本\n{analysis_text}",
            f"## 本页摘要（{extract.source_identity}）\n{extract.document_summary}",
        ]
        if p
    )
