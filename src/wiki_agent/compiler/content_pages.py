"""wiki 内容页集合——维护流程（提议、分配、link、核对）共用。"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.wiki.pages import CONTENT_DIRS


def all_content_slugs(wiki_dir: str | Path) -> list[str]:
    """全部可维护页的 slug（相对 wiki 根、无扩展名），排序稳定。"""
    wiki = Path(wiki_dir)
    slugs: list[str] = []
    for sub in CONTENT_DIRS:
        d = wiki / sub
        if not d.is_dir():
            continue
        # 保留层级：嵌套页 concepts/a/b.md 的 slug 是 concepts/a/b
        slugs.extend(
            str(p.relative_to(wiki).with_suffix("")) for p in sorted(d.rglob("*.md"))
        )
    return slugs


