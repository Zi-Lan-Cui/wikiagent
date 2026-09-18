"""结构重组的确定性链接改写（无 LLM）。

合并/删除时把 [[源]] 引用改指目标页或转纯文本。execute_merge/execute_delete 复用。
"""

from __future__ import annotations

import re

_LINK_RE = re.compile(r"\[\[([^\]]+?)(?:\|([^\]]+?))?\]\]")


def _rewrite_source_links(text: str, source_slug: str, target_slug: str) -> str:
    """源页正文内的自引用 → 指向合并后的目标页（内容搬家，链接跟着搬）。

    Args:
        text: 页面正文。
        source_slug: 源页 slug。
        target_slug: 目标页 slug。

    Returns:
        重写后的文本。
    """
    return _LINK_RE.sub(
        lambda m: (
            f"[[{target_slug}|{m.group(2)}]]"
            if m.group(1).strip() == source_slug and m.group(2)
            else f"[[{target_slug}]]"
            if m.group(1).strip() == source_slug
            else m.group(0)
        ),
        text,
    )


def _plain_source_links(text: str, source_slug: str, source_title: str) -> str:
    """目标页原正文对源页的引用 → 纯文本（合并后成了自链，转别名）。

    Args:
        text: 页面正文。
        source_slug: 源页 slug。
        source_title: 源页标题（别名兜底）。

    Returns:
        重写后的文本。
    """
    return _LINK_RE.sub(
        lambda m: (
            m.group(2)
            if m.group(1).strip() == source_slug and m.group(2)
            else source_title
            if m.group(1).strip() == source_slug
            else m.group(0)
        ),
        text,
    )
