"""落盘与机械收尾：写产出页、删消失页、全库链接转纯文本、index 维护。

全是代码动作，无 LLM。链接规则与旧 merge 的教训同源：
指向消失页的 [[slug|别名]] 转别名、[[slug]] 转标题末段——不许留悬空引用，
"无死链"的兑现点在这里，不靠扫描闸门（死链是 warning 级）。
"""

from __future__ import annotations

import re
from pathlib import Path

from wiki_agent.wiki.frontmatter import split_frontmatter

from .models import Unit
from .plan import UnitPlan


def _link_patterns(slug: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    escaped = re.escape(slug)
    alias = re.compile(rf"\[\[{escaped}\|([^\]]+?)\]\]")
    plain = re.compile(rf"\[\[{escaped}\]\]")
    return alias, plain


def rewrite_links_for_vanished(wiki_dir: Path, vanished: list[str]) -> int:
    """全库把指向消失页的 wikilink 转为纯文本（保留显示文字）。返回改写文件数。"""
    changed = 0
    for path in sorted(p for p in wiki_dir.rglob("*.md") if p.name != "index.md"):
        content = path.read_text(encoding="utf-8")
        new_content = content
        for slug in vanished:
            alias, plain = _link_patterns(slug)
            new_content = alias.sub(lambda m: m.group(1), new_content)
            new_content = plain.sub(slug.rsplit("/", 1)[-1], new_content)
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            changed += 1
    return changed


def pages_linking_to(wiki_dir: str | Path, slugs: list[str]) -> list[str]:
    """仍留在盘上、正文含指向 slugs 的 wikilink 的页——批尾 link 的范围输入。"""
    wiki_dir = Path(wiki_dir)
    targets = [re.compile(rf"\[\[{re.escape(s)}(?:\||\]\])") for s in slugs]
    hits: set[str] = set()
    for path in sorted(wiki_dir.rglob("*.md")):
        if path.name == "index.md":
            continue
        slug = f"{path.parent.name}/{path.stem}"
        if slug in set(slugs):
            continue
        content = path.read_text(encoding="utf-8")
        if any(rx.search(content) for rx in targets):
            hits.add(slug)
    return sorted(hits)


def _index_remove(wiki_dir: Path, slug: str) -> None:
    index = wiki_dir / "index.md"
    if not index.is_file():
        return
    lines = index.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if f"[[{slug}]]" not in ln]
    if len(kept) != len(lines):
        index.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _index_append(wiki_dir: Path, slug: str) -> None:
    index = wiki_dir / "index.md"
    existing = index.read_text(encoding="utf-8") if index.is_file() else ""
    if f"[[{slug}]]" in existing:
        return
    fm, _ = split_frontmatter((wiki_dir / f"{slug}.md").read_text(encoding="utf-8"))
    line = (
        f"- [[{slug}]] — [{fm.get('type', '')}] {slug}.md — {fm.get('title', slug.rsplit('/', 1)[-1])}"
    )
    if fm.get("summary"):
        line += f" — {fm['summary']}"
    index.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")


def apply_unit(
    wiki_dir: str | Path, unit: Unit, plan: UnitPlan, contents: dict[str, str]
) -> None:
    """落盘一个单元：产出页 → 删除消失页 → 链接与 index 收尾。

    前置的成文/装配产物都从内存取（contents 优先，缺省用草稿），
    所以调用方 normalize 后传入成品即可。
    """
    wiki_dir = Path(wiki_dir)
    for page in unit.out:
        text = contents.get(page.slug) or plan.drafts.get(page.slug) or ""
        (wiki_dir / f"{page.slug}.md").parent.mkdir(parents=True, exist_ok=True)
        (wiki_dir / f"{page.slug}.md").write_text(text.rstrip() + "\n", encoding="utf-8")
    for slug in unit.vanished:
        (wiki_dir / f"{slug}.md").unlink(missing_ok=True)
    if unit.vanished:
        rewrite_links_for_vanished(wiki_dir, unit.vanished)
    for slug in unit.vanished:
        _index_remove(wiki_dir, slug)
    for page in unit.out:
        if page.slug not in set(unit.in_pages):
            _index_append(wiki_dir, page.slug)
