"""compile 侧补充的确定性结构评分器：过度拆分 / 近重复 / 路由错误。

无新依赖（用字符二元组 Jaccard 近似相似，不引 embedding）。原则2：这些是【结果
结构】判定（产出页集合是否碎/重复/放错目录），不是路径。默认作为 A 层门禁的一部分。
"""

from __future__ import annotations

import re
from typing import Any

_ALLOWED_DIRS = ("concepts/", "entities/", "topics/", "sources/")
_STOP = re.compile(r"[`*_#>\-\[\](){}\"'，。、；：（）\s]+")


def _grams(text: str) -> set[str]:
    text = _STOP.sub("", text)
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _first_paragraph(body: str) -> str:
    for block in body.split("\n\n"):
        stripped = block.strip()
        if stripped and not stripped.startswith(("#", "```", "|", ">")):
            return stripped
    return body[:200]


def routing_violations(paths: list[str]) -> list[str]:
    """落在非内容目录（concepts/entities/topics/sources）之外的页 = 路由违规。"""
    return [p for p in paths if not p.startswith(_ALLOWED_DIRS)]


def over_split_pairs(
    pages: list[dict[str, Any]], *, threshold: float = 0.6
) -> list[tuple[str, str, float]]:
    """同一 source 下两页 title+首段 高度相似 = 过度拆分/近重复。

    page: {path, title, body, sources(list[str])}。返回违规对 (a, b, similarity)。
    """
    keyed: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        for src in page.get("sources", []) or []:
            keyed.setdefault(src, []).append(page)
    pairs: list[tuple[str, str, float]] = []
    seen: set[frozenset[str]] = set()
    for group in keyed.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.get("path") == b.get("path") or frozenset({a["path"], b["path"]}) in seen:
                    continue
                ga = _grams(f"{a.get('title','')} {_first_paragraph(a.get('body',''))}")
                gb = _grams(f"{b.get('title','')} {_first_paragraph(b.get('body',''))}")
                sim = _jaccard(ga, gb)
                if sim > threshold:
                    seen.add(frozenset({a["path"], b["path"]}))
                    pairs.append((a["path"], b["path"], round(sim, 2)))
    return pairs


def compile_structure_checks(
    pages: list[dict[str, Any]], threshold: float = 0.6
) -> dict[str, Any]:
    """A 层结构门禁：路由 + 过度拆分。passed = 无违规。"""
    paths = [str(p.get("path", "")) for p in pages]
    bad_routes = routing_violations(paths)
    dup_pairs = over_split_pairs(pages, threshold=threshold)
    return {
        "passed": not bad_routes and not dup_pairs,
        "bad_routes": bad_routes,
        "over_split": dup_pairs,
    }
