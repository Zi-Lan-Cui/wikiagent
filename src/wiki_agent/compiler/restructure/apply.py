"""落盘与机械收尾：写产出页、删消失页、全库链接转纯文本、index 维护。

全是代码动作，无 LLM。链接规则：指向消失页的 [[slug|别名]] 转别名、
[[slug]] 转标题末段——不许留悬空引用，"无死链"的兑现点在这里，
不靠扫描闸门（死链是 warning 级）。
"""

from __future__ import annotations

import re
from pathlib import Path

from wiki_agent.wiki.frontmatter import list_field, set_fields, split_frontmatter
from wiki_agent.wiki.pages import index_line

from .models import Unit
from .plan import UnitPlan


def _link_patterns(slug: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    escaped = re.escape(slug)
    alias = re.compile(rf"\[\[{escaped}\|([^\]]+?)\]\]")
    plain = re.compile(rf"\[\[{escaped}\]\]")
    return alias, plain


def _strip_related(text: str, vanished: set[str]) -> str:
    """frontmatter related 列表里指向消失页的条目整项移除。

    related 是引用清单不是散文——转纯文本会留下指向不存在页面的
    残项（扫描 error），必须删干净。条目兼容 "[[slug]]"、
    "[[slug|别名]]"、"slug" 三种存形，保留原形态写回。
    """
    if not vanished:
        return text
    fm, _ = split_frontmatter(text)
    items = list_field(fm.get("related"))
    if not items:
        return text

    def bare(item: str) -> str:
        return item.removeprefix("[[").removesuffix("]]").split("|")[0].strip()

    kept = [i for i in items if bare(i) not in vanished]
    if len(kept) == len(items):
        return text
    return set_fields(text, {"related": kept})


def _fm_end(text: str) -> int:
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    return m.end() if m else 0


def rewrite_links_for_vanished(wiki_dir: Path, vanished: list[str]) -> int:
    """全库清理指向消失页的引用：frontmatter related 整项移除，正文
    wikilink 转纯文本（保留显示文字）。返回改写文件数。"""
    changed = 0
    vset = set(vanished)
    for path in sorted(p for p in wiki_dir.rglob("*.md") if p.name != "index.md"):
        content = path.read_text(encoding="utf-8")
        new_content = _strip_related(content, vset)
        head_end = _fm_end(new_content)
        head, body = new_content[:head_end], new_content[head_end:]
        for slug in vanished:
            alias, plain = _link_patterns(slug)
            body = alias.sub(lambda m: m.group(1), body)
            body = plain.sub(slug.rsplit("/", 1)[-1], body)
        new_content = head + body
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
    index.write_text(
        existing.rstrip()
        + "\n"
        + index_line(
            slug,
            str(fm.get("type", "")),
            str(fm.get("title", slug.rsplit("/", 1)[-1])),
            " ".join(str(fm.get("summary", "")).splitlines()),
            " ".join(str(fm.get("goal", "")).splitlines()),
        )
        + "\n",
        encoding="utf-8",
    )


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
