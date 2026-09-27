"""wiki 内容页名册——维护侧（提议、分配、link、核对）共用的页面集合。"""

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
        slugs.extend(f"{sub}/{p.stem}" for p in sorted(d.rglob("*.md")))
    return slugs


def page_path(wiki_dir: str | Path, slug: str) -> Path:
    """slug → 页面路径。slug 必须是名册成员（白名单，杜绝路径穿越）。"""
    if slug not in set(all_content_slugs(wiki_dir)):
        raise ValueError(f"不是可维护的 wiki 页: {slug!r}")
    return Path(wiki_dir) / f"{slug}.md"
