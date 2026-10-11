"""单元核对与装配（执行时）：verify → 章节切分 → 路由 → 草稿。

prepare_unit 的全部失败都发生在写盘之前：抛错即单元失败，工作区无改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wiki_agent.wiki.frontmatter import list_field, split_frontmatter
from wiki_agent.wiki.pages import PAGE_TYPE_BY_DIR, all_content_slugs, path_for
from wiki_agent.wiki.sections import Section, page_sections

from .models import OutPage, Unit, UnitMismatchError
from .resolve import validate_unit
from .route import route_unit


@dataclass
class PreparedUnit:
    """prepare_unit 的产出：仅含后续成文/落盘所需资料。

    sections 与完整 assignment 是构造草稿的中间态，不随结果带出；
    审计进 job detail 的只有两个数量。
    """

    drafts: dict[str, str] = field(default_factory=dict)  # out slug → 装配草稿全文
    old_content: dict[str, str] = field(default_factory=dict)
    old_meta: dict[str, dict] = field(default_factory=dict)
    sibling_slugs: dict[str, list[str]] = field(default_factory=dict)
    final_slugs: set[str] = field(default_factory=set)
    assignment_count: int = 0
    fixed_by_take_count: int = 0  # take 直接指定的章节归属数（审计用）


def _existing_slugs(wiki_dir: Path) -> set[str]:
    # 页面列表统一由 wiki.pages 的 CONTENT_DIRS 提供（含嵌套层级），不在本模块重复实现
    return set(all_content_slugs(wiki_dir))


def _frontmatter_for(wiki_dir: Path, page: OutPage) -> dict:
    path = path_for(wiki_dir, page.slug)
    if path.is_file():
        frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        return frontmatter
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
    """按归属把章节正文装配成每个 out 页的草稿，保持输入页顺序。"""
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


async def prepare_unit(wiki_dir: str | Path, unit: Unit, llm: Any) -> PreparedUnit:
    """核对声明、切分章节、计算分配、装配草稿。全程只读。

    Raises:
        UnitMismatchError: in 页缺失或声明与当前 wiki 状态不符（排队期间页面有变化）。
        RouteError: 章节分配校验失败（漏配/重配等）。
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
        sections.extend(page_sections(path_for(wiki_dir, slug), slug))
    assignment, fixed = await route_unit(llm, unit, sections)
    plan = PreparedUnit(
        drafts=_draft(unit, sections, assignment),
        final_slugs=(existing - set(unit.vanished)) | set(unit.out_slugs),
        assignment_count=len(assignment),
        fixed_by_take_count=len(fixed),
    )
    # 溯源并集：out 页的 sources = 全部 in 页 sources 的并集，
    # 否则被消费页的源文件会从溯源链消失
    unit_sources: list[str] = []
    for slug in unit.in_pages:
        frontmatter, _ = split_frontmatter(path_for(wiki_dir, slug).read_text(encoding="utf-8"))
        for s in list_field(frontmatter.get("sources")):
            if s not in unit_sources:
                unit_sources.append(s)
    for page in unit.out:
        path = path_for(wiki_dir, page.slug)
        plan.old_content[page.slug] = (
            path.read_text(encoding="utf-8") if path.is_file() else ""
        )
        plan.old_meta[page.slug] = _frontmatter_for(wiki_dir, page)
        if unit_sources:
            plan.old_meta[page.slug]["sources"] = unit_sources
        plan.sibling_slugs[page.slug] = [
            p.slug for p in unit.out if p.slug != page.slug
        ]
    return plan
