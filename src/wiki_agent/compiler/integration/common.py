"""集成层共享工具——跨阶段复用的 index/slug 读取。"""

from __future__ import annotations

import re
from pathlib import Path

from wiki_agent.wiki.pages import CONTENT_DIRS, slug_from_ref

# 内容目录白名单由 PAGE_TYPE_BY_DIR 派生——新增内容目录只改 pages 一处
_INDEX_SLUG_RE = re.compile(rf"^(?:{'|'.join(CONTENT_DIRS)})/")


def extract_slugs_from_index(index_content: str) -> set[str]:
    """从 wiki/index.md 提取所有已有页面 slug。

    只收集 CONTENT_DIRS 各目录下的 `[[dir/xxx]]` 引用。

    Args:
        index_content: index.md 内容。

    Returns:
        slug 集合（不含 .md 后缀）。
    """
    slugs: set[str] = set()
    for m in re.finditer(r"\[\[([a-zA-Z0-9][^\]]+?)\]\]", index_content):
        slug = m.group(1).strip()
        if _INDEX_SLUG_RE.match(slug):
            slugs.add(slug_from_ref(slug))
    return slugs


def load_valid_slugs(wiki_dir: str | Path) -> set[str]:
    """从 wiki/index.md 读取已有页面 slug 集合。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        已有页面 slug 集合；index 缺失时返回空集。
    """
    try:
        return extract_slugs_from_index((Path(wiki_dir) / "index.md").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
