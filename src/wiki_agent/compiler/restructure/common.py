"""结构重组的工具——页面读取 / 证据收集 / LLM 输出安全解析。

代码只做这些（收集证据），不做相似度阈值——裁决全给 LLM。
被 proposal/review/execute 复用；自身只依赖 models + 底层
（wiki.frontmatter / integration.parse），不 import 任何同级流程模块。
"""

from __future__ import annotations

import json
from pathlib import Path

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.compiler.restructure.models import _CONTENT_DIRS, Proposal
from wiki_agent.log import emit_event, get_logger
from wiki_agent.wiki.frontmatter import split_frontmatter

logger = get_logger("RESTRUCTURE")


def _load_pages(wiki_dir: str | Path) -> dict[str, dict]:
    """读全库页面。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        {slug: {path, summary, body, related, title}} 映射。
    """
    wiki = Path(wiki_dir)
    pages: dict[str, dict] = {}
    for sub in _CONTENT_DIRS:
        d = wiki / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            slug = str(p.relative_to(wiki)).replace(".md", "")
            content = p.read_text(encoding="utf-8")
            fm, body = split_frontmatter(content)
            pages[slug] = {
                "path": p,
                "frontmatter": fm,
                "title": fm.get("title", slug),
                "summary": fm.get("summary", ""),
                "body": body.strip(),  # frontmatter 层不 strip，调用方按需
                "related": fm.get("related", ""),
            }
    return pages


def _filter_valid_pages(
    proposals: list[Proposal],
    pages: dict[str, dict],
) -> list[Proposal]:
    """过滤 LLM 幻觉 slug——所有涉及页面必须真实存在，否则丢弃提议。

    Args:
        proposals: 原始提议列表。
        pages: 页面表。

    Returns:
        只含真实页面的提议。
    """
    valid: list[Proposal] = []
    for p in proposals:
        missing = [s for s in p.pages if s not in pages]
        if missing:
            logger.warning("  ✗ 提议引用不存在的页面 %s——丢弃", missing)
            emit_event("restructure_invalid_slug", pages=p.pages, missing=missing, op=p.op)
            continue
        valid.append(p)
    return valid


def _index_overview(wiki_dir: str | Path) -> str:
    """全库紧凑视野——slug + title + summary（粗提的输入）。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        逐行索引文本。
    """
    pages = _load_pages(wiki_dir)
    lines = []
    for slug in sorted(pages):
        p = pages[slug]
        lines.append(f"- [[{slug}]] — {p['title']}{' — ' + p['summary'] if p['summary'] else ''}")
    return "\n".join(lines)


def _incoming_links(pages: dict[str, dict], slug: str) -> list[str]:
    """收集谁引用了 slug——复判的证据（代码收集，LLM 裁决）。

    Args:
        pages: 页面表。
        slug: 被引用页面。

    Returns:
        引用方 slug 列表。
    """
    incoming = []
    for other, page in pages.items():
        if other == slug:
            continue
        if f"[[{slug}]]" in page["body"] or f"[[{slug}|" in page["body"]:
            incoming.append(other)
    return incoming


def _safe_parse_json(content: str):
    """剥 fence + loads——失败返回 None（不崩）。

    retry 的契约是"返回最后一次响应（即使校验未通过）"——
    check 通过与否，解析都可能拿到坏内容（超长截断/重试穷尽）。
    二次校验防护是每个 LLM 调用点的义务（plan 静默失败同款 bug 教训）。

    Args:
        content: LLM 原始输出。

    Returns:
        解析后的 JSON；解析失败返回 None。
    """
    # fence 剥离统一走 integration.parse._strip_fence（含 I5 尾部括号 repair）
    cleaned = _strip_fence(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("  JSON 二次解析失败（check 已通过但内容仍坏）: %s", str(e)[:120])
        return None
