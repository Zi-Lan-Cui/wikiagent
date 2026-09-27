"""单元核对与装配（执行时）：verify → 章节化 → 路由 → 草稿。

prepare_unit 一切失败都发生在写盘之前——抛错即单元 failed，工作区无改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wiki_agent.wiki.frontmatter import list_field, split_frontmatter
from wiki_agent.wiki.pages import PAGE_TYPE_BY_DIR
from wiki_agent.wiki.sections import Section, page_sections

from .models import OutPage, Unit, UnitMismatchError
from .resolve import validate_unit
from .route import route_unit


@dataclass
class UnitPlan:
    unit: Unit
    sections: list[Section]
    assignment: dict[str, str] = field(default_factory=dict)
    fixed: dict[str, str] = field(default_factory=dict)  # take 直给的归属（审计）
    drafts: dict[str, str] = field(default_factory=dict)  # out slug → 毛坯全文
    old_content: dict[str, str] = field(default_factory=dict)
    old_meta: dict[str, dict] = field(default_factory=dict)
    sibling_titles: dict[str, list[str]] = field(default_factory=dict)
    final_slugs: set[str] = field(default_factory=set)


def _existing_slugs(wiki_dir: Path) -> set[str]:
    return {
        f"{p.parent.name}/{p.stem}"
        for d in PAGE_TYPE_BY_DIR
        for p in (wiki_dir / d).rglob("*.md")
        if p.parent == wiki_dir / d
    }


def _frontmatter_for(wiki_dir: Path, page: OutPage) -> dict:
    path = wiki_dir / f"{page.slug}.md"
    if path.is_file():
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        return fm
    directory = page.slug.split("/", 1)[0]
    title = page.slug.rsplit("/", 1)[-1]
    return {
        "type": PAGE_TYPE_BY_DIR.get(directory, "concept"),
        "title": title,
        "summary": page.intent[:80] or title,
        "goal": page.intent or f"{title}",
        "related": "[]",
    }


def _draft(unit: Unit, sections: list[Section], assignment: dict[str, str]) -> dict[str, str]:
    """按归属把章节正文装配成每个 out 页的毛坯（保持输入页顺序）。"""
    drafts: dict[str, str] = {}
    for page in unit.out:
        chunks: list[str] = []
        for section in sections:
            if assignment.get(section.id) == page.slug:
                chunks.append(
                    section.body if not section.heading else f"## {section.heading}\n\n{section.body}"
                )
        drafts[page.slug] = "\n\n".join(c for c in chunks if c.strip())
    return drafts


async def prepare_unit(wiki_dir: str | Path, unit: Unit, llm: Any) -> UnitPlan:
    """核对声明、切分章节、计算分配、装配毛坯。全程只读。

    Raises:
        UnitMismatchError: in 页缺失——排队期间世界变了。
        RouteError: 分配不守恒。
    """
    wiki_dir = Path(wiki_dir)
    existing = _existing_slugs(wiki_dir)
    missing = [s for s in unit.in_pages if s not in existing]
    if missing:
        raise UnitMismatchError(missing=missing)
    reason = validate_unit(unit, existing)
    if reason:
        raise UnitMismatchError(detail=f"声明与盘面不符: {reason}")

    sections: list[Section] = []
    for slug in unit.in_pages:
        sections.extend(page_sections(wiki_dir / f"{slug}.md", slug))
    plan = UnitPlan(unit=unit, sections=sections)
    plan.assignment, plan.fixed = await route_unit(llm, unit, sections)
    plan.drafts = _draft(unit, sections, plan.assignment)
    plan.final_slugs = (existing - set(unit.vanished)) | set(unit.out_slugs)
    # 溯源并集：合并/改名后 out 页的来源 = 全部 in 页 sources 的并集，
    # 否则被消费页的源文件从溯源链消失（真实执行轮抓出的缺陷）
    unit_sources: list[str] = []
    for slug in unit.in_pages:
        fm, _ = split_frontmatter((wiki_dir / f"{slug}.md").read_text(encoding="utf-8"))
        for s in list_field(fm.get("sources")):
            if s not in unit_sources:
                unit_sources.append(s)
    for page in unit.out:
        path = wiki_dir / f"{page.slug}.md"
        plan.old_content[page.slug] = (
            path.read_text(encoding="utf-8") if path.is_file() else ""
        )
        plan.old_meta[page.slug] = _frontmatter_for(wiki_dir, page)
        if unit_sources:
            plan.old_meta[page.slug]["sources"] = unit_sources
        plan.sibling_titles[page.slug] = [
            p.slug for p in unit.out if p.slug != page.slug
        ]
    return plan
