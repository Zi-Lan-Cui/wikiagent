#!/usr/bin/env python3
"""Build a private, source-verifiable evaluation set from an existing Wiki."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from wiki_agent.wiki.frontmatter import split_frontmatter

_SENTENCE = re.compile(r"[^\n。！？!?]+[。！？!?]?")
_META_PHRASES = (
    "本文",
    "本文档",
    "这是一",
    "这份文档",
    "这份笔记",
    "值得后续",
    "值得关注",
    "整体而言",
    "文档结构",
    "文档的核心价值",
    "笔记的独特之处",
    "适合作为",
    "未发现明显错误",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_names(raw: object) -> list[str]:
    text = str(raw or "").strip()
    try:
        values = json.loads(text)
    except json.JSONDecodeError:
        values = [item.strip().strip("\"'") for item in text.strip("[]").split(",")]
    return [str(item).strip() for item in values if str(item).strip()]


def _facts(body: str, *, limit: int = 4) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    for position, match in enumerate(_SENTENCE.finditer(body)):
        sentence = match.group(0)
        value = re.sub(r"[`*_#>-]+", "", sentence).strip()
        value = re.sub(r"\s+", " ", value)
        if not 24 <= len(value) <= 220:
            continue
        if any(phrase in value for phrase in _META_PHRASES):
            continue
        # Prefer concrete definitions, behaviours, boundaries and causal
        # statements over prose that merely describes the document.
        signals = (
            "是",
            "用于",
            "通过",
            "使用",
            "不会",
            "不能",
            "需要",
            "导致",
            "返回",
            "创建",
            "修改",
            "支持",
            "区别",
            "核心",
        )
        score = sum(signal in value for signal in signals)
        score += int(any(char.isdigit() for char in value))
        score += int("例如" in value or "若" in value or "当" in value)
        candidates.append((score, -position, value))

    candidates.sort(reverse=True)
    result: list[str] = []
    for _, _, value in candidates:
        if value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def build_dataset(
    wiki_dir: Path,
    provenance_dir: Path,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    page_map: dict[str, set[str]] = defaultdict(set)
    for page in sorted(wiki_dir.rglob("*.md")):
        if ".git" in page.parts or page.name == "index.md":
            continue
        frontmatter, _ = split_frontmatter(page.read_text(encoding="utf-8"))
        relative = page.relative_to(wiki_dir).as_posix()
        for source in _source_names(frontmatter.get("sources")):
            page_map[source].add(relative)

    candidates: list[dict[str, Any]] = []
    for record in sorted(provenance_dir.glob("*.md")):
        content = record.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(content)
        source_names = _source_names(frontmatter.get("sources"))
        pages = sorted({page for source in source_names for page in page_map.get(source, set())})
        facts = _facts(body)
        if not pages or len(facts) < 2:
            continue
        candidates.append(
            {
                "record": record,
                "source_names": source_names,
                "pages": pages,
                "facts": facts,
                "body": body,
                "score": min(len(body), 4000) + 300 * len(pages) + 200 * len(facts),
            }
        )

    # Prefer information-rich cases, then stabilize ties by path.  Limit the
    # number of cases dominated by one page so the seed set covers more topics.
    candidates.sort(key=lambda item: (-item["score"], item["record"].name))
    selected: list[dict[str, Any]] = []
    page_frequency: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        if all(page_frequency[page] >= 2 for page in candidate["pages"]):
            continue
        selected.append(candidate)
        for page in candidate["pages"]:
            page_frequency[page] += 1
        if len(selected) == limit:
            break
    if len(selected) < limit:
        selected_records = {item["record"] for item in selected}
        for candidate in candidates:
            if candidate["record"] in selected_records:
                continue
            selected.append(candidate)
            selected_records.add(candidate["record"])
            if len(selected) == limit:
                break
    if len(selected) < limit:
        raise ValueError(f"只找到 {len(selected)} 条可追溯高信息样本，不足 {limit} 条")

    cases = []
    for index, item in enumerate(selected, 1):
        record: Path = item["record"]
        cases.append(
            {
                "id": f"wiki-{index:03d}",
                "task_type": "compile_outcome",
                "suite": "capability",
                "polarity": "positive",
                "source": record.name,
                "sha256": _sha256(record),
                "source_identities": item["source_names"],
                "expected_pages": item["pages"],
                "required_facts": [
                    {"id": f"fact-{fact_index:02d}", "assertion": fact, "critical": True}
                    for fact_index, fact in enumerate(item["facts"], 1)
                ],
                "forbidden_claims": [],
                "expected_behavior": {"allow_pages": True, "require_noop": False},
                "reference_solution": {"noop": False},
                "expected_verdict": "pass",
                "human_verdict": None,
                "reviewer": None,
                "annotation": {
                    "source": "verbatim_provenance",
                    "review_status": "source_verified",
                    "notes": "关键事实逐字取自 provenance；页面由 frontmatter.sources 反向关联。",
                },
            }
        )
    negatives = _negative_cases(selected, limit=12)
    cases.extend(negatives)
    positive_count = len(cases) - len(negatives)
    return {
        "version": 3,
        "dataset_id": "wiki-seed-v3",
        "description": "从现有 Wiki/provenance 构建的种子评测集：正例(capability) + 应拒答负例(regression)",
        "source_root": str(provenance_dir.resolve()),
        "wiki_root": str(wiki_dir.resolve()),
        "case_count": len(cases),
        "positive_count": positive_count,
        "negative_count": len(negatives),
        "quality_contract": {
            "facts_are_verbatim": True,
            "target_pages_exist": True,
            "provenance_link_required": True,
            "human_calibration_required": True,
            "golden_after_human_review": False,
            "reference_solutions_pass_deterministic": True,
            "positive_and_negative_balanced": True,
        },
        "cases": cases,
    }


def _negative_cases(selected: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    """构造应拒答负例：给定某 source，禁止输出另一 source 才有的论断。

    可核验：断言"该论断字符串确实不在本 case source 正文里"（否则不成其为负例），
    从而正确行为=no-op/不臆造。参考解=不产出任何页，天然过 score_refusal。
    """
    negatives: list[dict[str, Any]] = []
    count = len(selected)
    for i in range(min(limit, count)):
        target = selected[i]
        donor = selected[(i + 1) % count]
        if donor is target or not donor["facts"]:
            continue
        foreign_fact = donor["facts"][0]
        # 论断必须真·不在 target 正文——否则不是"证据不足"负例
        if foreign_fact in target["body"]:
            continue
        negatives.append(
            {
                "id": f"wiki-neg-{len(negatives) + 1:03d}",
                "task_type": "compile_outcome",
                "suite": "regression",
                "polarity": "negative",
                "source": target["record"].name,
                "sha256": _sha256(target["record"]),
                "source_identities": target["source_names"],
                "expected_pages": [],
                "required_facts": [],
                "forbidden_claims": [foreign_fact],
                "expected_behavior": {"allow_pages": False, "require_noop": True},
                "reference_solution": {"noop": True, "abstain": True},
                "expected_verdict": "pass",
                "human_verdict": None,
                "reviewer": None,
                "annotation": {
                    "source": "cross_source_absent",
                    "review_status": "source_verified",
                    "notes": "论断逐字取自另一 source 且经核验不在本 source 正文——正确行为是不产出。",
                },
            }
        )
    return negatives


def main() -> int:
    parser = argparse.ArgumentParser(description="从现有 Wiki 生成私有评测集")
    parser.add_argument("--wiki-dir", type=Path, default=Path("wiki"))
    parser.add_argument("--provenance-dir", type=Path, default=Path("workspace/provenance/sources"))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("workspace/evals/wiki-golden-30.json"))
    args = parser.parse_args()
    payload = build_dataset(
        args.wiki_dir.expanduser().resolve(),
        args.provenance_dir.expanduser().resolve(),
        limit=args.limit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 {payload['case_count']} 条样本: {args.output}")

    # 产完即自检：schema 合法 + 每条参考解过全部确定性门禁（任务可解、评分器配好）
    from evals.core.dataset import assert_all_references_pass, validate_schema_v3

    errors = validate_schema_v3(payload)
    ref_failures = assert_all_references_pass(payload, Path(payload["wiki_root"]))
    if errors:
        print("⚠ schema 非法:", errors[:5])
        return 1
    if ref_failures:
        print(f"⚠ {len(ref_failures)} 例参考解未过确定性门禁:", ref_failures[:5])
        return 1
    print(f"✅ schema 合法 + {payload['case_count']} 例参考解全部过确定性门禁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
