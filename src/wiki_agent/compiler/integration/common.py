"""集成层共享工具——跨阶段复用的 index/slug 读取。"""

from __future__ import annotations

import re
from pathlib import Path


def extract_slugs_from_index(index_content: str) -> set[str]:
    """从 wiki/index.md 提取所有已有页面 slug。

    匹配 `[[entities/xxx]]`、`[[concepts/xxx]]`、`[[topics/xxx]]` 格式。

    Args:
        index_content: index.md 内容。

    Returns:
        slug 集合（不含 .md 后缀）。
    """
    slugs: set[str] = set()
    for m in re.finditer(r"\[\[([a-zA-Z0-9][^\]]+?)\]\]", index_content):
        slug = m.group(1).strip()
        # 只收集 entities/、concepts/、topics/ 下的 slug，不含 .md
        if re.match(r"^(entities|concepts|topics)/", slug):
            slugs.add(slug.replace(".md", ""))
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
