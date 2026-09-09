"""Small, outcome-focused metric set for LLM Wiki evaluations."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.quality import check_page_quality


def semantic_metrics(judgement: dict[str, Any]) -> dict[str, Any]:
    claims = judgement.get("grounding", {}).get("claims", [])
    facts = judgement.get("coverage", {}).get("facts", [])
    supported = sum(item.get("verdict") == "supported" for item in claims)
    unsupported = sum(item.get("verdict") == "unsupported" for item in claims)
    grounding_unknown = sum(item.get("verdict") == "unknown" for item in claims)
    covered = sum(item.get("verdict") == "covered" for item in facts)
    missing = sum(item.get("verdict") == "missing" for item in facts)
    coverage_unknown = sum(item.get("verdict") == "unknown" for item in facts)
    grounding_denominator = supported + unsupported
    coverage_denominator = covered + missing
    return {
        "groundedness": supported / grounding_denominator if grounding_denominator else None,
        "coverage": covered / coverage_denominator if coverage_denominator else None,
        "unsupported_claims": unsupported,
        "missing_facts": missing,
        "unknown": grounding_unknown + coverage_unknown,
    }


def reliability_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate pass@1 and per-case pass^k without averaging away failures."""
    grouped: dict[str, list[bool]] = defaultdict(list)
    for run in runs:
        grouped[str(run["case_id"])].append(run.get("verdict") == "pass")
    attempts = sum(len(values) for values in grouped.values())
    return {
        "cases": len(grouped),
        "attempts": attempts,
        "pass_at_1": sum(sum(values) for values in grouped.values()) / attempts
        if attempts
        else 0.0,
        "pass_power_k": (
            sum(all(values) for values in grouped.values()) / len(grouped) if grouped else 0.0
        ),
        "k_by_case": {case_id: len(values) for case_id, values in grouped.items()},
    }


def integrity_metrics(
    wiki_dir: Path,
    pages: list[dict[str, str]],
    *,
    allowed_source_identities: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministically verify final target pages, provenance, and (optionally)
    source identity (the deterministic "authority" leg: every cited source must be
    one of the case's designated identities)."""
    valid_slugs = {
        path.relative_to(wiki_dir).with_suffix("").as_posix()
        for path in wiki_dir.rglob("*.md")
        if ".git" not in path.parts
    }
    allowed = set(allowed_source_identities or ())
    errors: list[dict[str, str]] = []
    missing_provenance: list[str] = []
    for item in pages:
        relative = str(item.get("path", "")).removeprefix("wiki/")
        path = wiki_dir / relative
        if not path.is_file():
            errors.append({"path": relative, "reason": "target page missing"})
            continue
        content = path.read_text(encoding="utf-8")
        frontmatter, _ = split_frontmatter(content)
        cited = _parse_source_list(frontmatter.get("sources"))
        if not cited:
            missing_provenance.append(relative)
        # 来源同一性(authority)腿：页面可正当聚合多来源，只要求"至少引用一个
        # 指定来源身份"（该页确由本 case 材料派生），而非"全部来源∈指定集"。
        if allowed and cited and not (set(cited) & allowed):
            errors.append(
                {"path": relative, "reason": "provenance 未引用任何指定来源身份"}
            )
        for issue in check_page_quality(content, path=relative, valid_slugs=valid_slugs):
            if issue.level == "error":
                errors.append({"path": relative, "reason": issue.message})
    return {
        "passed": not errors and not missing_provenance and bool(pages),
        "checked_pages": len(pages),
        "errors": errors,
        "missing_provenance": missing_provenance,
    }


def _parse_source_list(raw: object) -> list[str]:
    """Read a frontmatter ``sources`` value (JSON list or ``[a, b]`` text) to names."""
    text = str(raw or "").strip()
    if not text.strip("[] "):
        return []
    try:
        values = json.loads(text)
    except json.JSONDecodeError:
        values = [item.strip().strip("\"'") for item in text.strip("[]").split(",")]
    if not isinstance(values, list):
        return [text]
    return [str(item).strip() for item in values if str(item).strip()]
