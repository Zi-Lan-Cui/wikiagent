"""维护流水线的 LLM 提示词：提议、复核、路由、成文。

输出契约统一为 JSON，校验函数与各 prompt 同处一文件；
解析与重试由调用方负责。
"""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.rules import count_unclosed_fences
from wiki_agent.wiki.sections import TOP_LABEL, Section, pick_gist_limit

from .models import Unit


def section_outline(entries: list[tuple[str, str, list[str]]]) -> str:
    """复核用的章节骨架行：slug — title — 章节: 标题1；标题2…

    章节是判断"两页是否复述同一事物""页面是否内容驳杂"的主要信号，
    只随复核输入：复核只看单元涉及页，提议阶段看全库，逐页附章节
    会大幅压缩可处理的库规模。
    """
    lines = []
    for slug, title, headings in entries:
        shown = "；".join(headings[:8]) + ("…" if len(headings) > 8 else "")
        lines.append(f"- {slug} — {title} — sections: {shown or '(no subsections)'}")
    return "\n".join(lines)


PROPOSE_SYSTEM = (
    "You are the proposer for wiki structure maintenance. Given the outline of"
    " all pages, find structural problems and produce restructure units: each"
    " unit declares input pages (in, consumed whole) and output pages (out,"
    " slug + intent stating what that page will cover). Merge = N→1, split/new"
    " = 1→M, rewrite = A→A (intent gives the direction), delete = empty out."
    " Introduce no new knowledge: all content must come from the in pages."
    " Decide merge vs split against the organization rules: different facets of"
    " one thing are not redundancy, and different page types (concept/entity/"
    " topic) each have their place. Output an empty array when no structure is"
    ' worth changing. Output JSON only: {"units":[{"in_pages":[...],'
    '"out":[{"slug":"concepts/x","intent":"..."}],"reason":"..."}]}'
)


def propose_user(index_content: str, schema: str = "", purpose: str = "") -> str:
    parts = []
    if purpose:
        parts.append(f"## Knowledge base mission\n{purpose}")
    if schema:
        parts.append(f"## Organization rules\n{schema}")
    # index 行已携带 title/summary/goal，不再附页面大纲节：
    # 重复字段会稀释注意力，也压低可处理的库规模
    parts += [f"## Table of contents\n{index_content}", "Give the restructure units (may be empty)."]
    return "\n\n".join(parts)


def check_propose_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, list):
        return False, 'missing units array — output {"units": [...]}.'
    for i, raw in enumerate(units):
        if not isinstance(raw, dict) or not isinstance(raw.get("in_pages"), list) or not raw["in_pages"]:
            return False, f"units[{i}] needs a non-empty in_pages"
        if not isinstance(raw.get("out"), list):
            return False, f"units[{i}] needs an out array (empty array for delete)"
    return True, ""


RECHECK_SYSTEM = (
    "You are the human-review proxy for structural decisions. Given one"
    " restructure unit and the outline of the pages it touches, judge whether"
    " the restructure holds now and its direction is right (should these be"
    " merged / split this way). Structural decisions have wide impact — prefer"
    ' dropping to guessing. Output JSON only: {"keep": true|false,'
    ' "reason": "..."}'
)


def recheck_user(unit: Unit, outline: str, schema: str = "") -> str:
    intents = "；".join(f"{p.slug}: {p.intent or '(carry over)'}" for p in unit.out) or "(whole page deleted)"
    parts = [
        f"## Unit\nconsumes {unit.in_pages} → produces [{intents}]\nreason: {unit.reason}",
        f"## Outline of touched pages\n{outline}",
    ]
    if schema:
        parts.insert(0, f"## Organization rules\n{schema}")
    parts.append("keep or drop?")
    return "\n\n".join(parts)


def check_recheck_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("keep"), bool):
        return False, 'needs {"keep": true/false, "reason": "..."}.'
    return True, ""


ROUTE_SYSTEM = (
    "You are a section allocator. Given the section outline of the input pages"
    " and the intent of each output page, assign every section to exactly one"
    " output page (for a delete unit mark all dropped — only allowed when the"
    " output is empty and the delete was explicitly proposed). Never drop a"
    " section, never assign one section twice, never invent a target that does"
    ' not exist. Output JSON only: {"assign": [{"section": "<section id>",'
    ' "to": "<out slug>"}]}. section must copy verbatim the section id at the'
    " start of each outline line (including :: and §top), excluding the title"
    " or gist after the pipe. to must be a full slug from the given output list"
    " (including the directory prefix such as concepts/); never write back an"
    " input page's own slug — when an input page is not an output page, its"
    " sections go only to output pages."
)


# 路由大纲摘要预算：常规档每节 160 字符（两段开头）；
# 节数超过 20 用短档（首句 40 字符），控制提示规模。
ROUTE_GIST_CHARS = 160
ROUTE_GIST_SHORT_CHARS = 40
ROUTE_OUTLINE_MAX_GIST_SECTIONS = 20


def route_outline(sections: list[Section]) -> str:
    limit = pick_gist_limit(
        len(sections), ROUTE_GIST_CHARS, ROUTE_GIST_SHORT_CHARS, ROUTE_OUTLINE_MAX_GIST_SECTIONS
    )
    return "\n".join(f"- {s.id} | {s.heading or TOP_LABEL} | {s.gist(limit)}" for s in sections)


def route_user(unit: Unit, sections: list[Section], fixed_note: str) -> str:
    intents = "；".join(f"{p.slug}: {p.intent}" for p in unit.out if not p.take)
    return (
        f"## Output page intents\n{intents or '(no pages to allocate to)'}\n\n"
        f"## Section outline\n{route_outline(sections)}\n\n{fixed_note}"
    )


def check_route_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    assign = data.get("assign") if isinstance(data, dict) else None
    if not isinstance(assign, list):
        return False, 'missing assign array — output {"assign": [...]}.'
    for i, item in enumerate(assign):
        if not isinstance(item, dict) or not item.get("section") or not item.get("to"):
            return False, f"assign[{i}] needs both section and to"
    return True, ""


REWRITE_SYSTEM = (
    "You are the wiki page writer. Given the assembled draft (sections moved"
    " per the allocation), the page's intent and the old body, rewrite into a"
    " coherent page: use only the given material, add no new facts, keep every"
    " [[link]] and image reference, and output complete markdown including the"
    " frontmatter. The frontmatter must carry all of type, title, summary and"
    " goal (inherit from the old version; a new page inherits from the draft's"
    " frontmatter — never omit them). Write every link as [[slug|display text]"
    " — a bare [[slug]] is forbidden; every ```python code block must close"
    " with a bare ```. If nothing needs changing, return the draft unchanged."
    " Keep all text in the page's original language; never translate."
)


def check_rewrite_page(content: str) -> tuple[bool, str]:
    """成文输出形状预检，口径与 wiki.rules 质量检查一致。

    带 frontmatter 的输出检查必填字段；不带头的视为正文片段放行——
    模型对拆分新页常只回正文，整页由 _fill_frontmatter 按骨架合成，
    在重试层拒掉只会把可用的兜底变成整单元失败。两种形态都要求代码块
    闭合；--- 开头但解析不了或缺必填字段的照旧拒。
    """
    body = content.strip()
    if not body:
        return False, "empty output."
    if not body.startswith("---"):
        if count_unclosed_fences(body):
            return False, "unclosed code block — every ```python opener needs a matching bare ```."
        return True, ""
    try:
        frontmatter, _ = split_frontmatter(body)
    except Exception:
        return False, "frontmatter unparseable — check the --- are paired."
    missing = [k for k in ("type", "title", "summary", "goal") if not str(frontmatter.get(k) or "").strip()]
    if missing:
        return False, f"frontmatter missing {missing} — all four required fields must be present."
    if count_unclosed_fences(body):
        return False, "unclosed code block — every ```python opener needs a matching bare ```."
    return True, ""


def rewrite_user(
    slug: str, intent: str, draft: str, old: str, siblings: list[str]
) -> str:
    parts = [f"## Target page\n{slug}\nintent: {intent or '(assemble the draft into prose)'}"]
    if siblings:
        parts.append("## Pages from this same batch\n" + "、".join(siblings))
    if old:
        parts.append(f"## Old version (structure and frontmatter baseline)\n{old[:12000]}")
    parts.append(f"## Draft (full content after allocation)\n{draft[:30000]}")
    parts.append("Output the rewritten complete page.")
    return "\n\n".join(parts)


def json_of(content: str) -> dict[str, Any]:
    data = json.loads(content)
    assert isinstance(data, dict)
    return data
