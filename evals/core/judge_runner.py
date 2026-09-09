"""单例评测运行的复用内核：给定 client + case + 语料，产出【分层】结果态 verdict。

snapshot 与 agreement 共用，避免两处漂移。只评结果（outcome），不评路径。
最终 verdict = A ∧ B：A = 确定性结构门禁（integrity / 对抗例 score_adversarial），
B = LLM 语义 judge；两层都判好才 pass（review/unknown 计不通过）。单层结论一并回传，
供报告出 per-layer + combined。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from evals.core.compile_graders import compile_structure_checks
from evals.core.outcomes import integrity_metrics, semantic_metrics
from evals.judges.semantic_judge import judge_case
from wiki_agent.wiki.frontmatter import split_frontmatter


def _structure_page(p: dict[str, str]) -> dict[str, Any]:
    fm, body = split_frontmatter(p.get("content", ""))
    sources = fm.get("sources")
    if isinstance(sources, str):
        sources = [s for s in re.findall(r"[\w.\-]+\.md", sources)]
    return {"path": p.get("path", ""), "title": str(fm.get("title", "")), "body": body,
            "sources": sources or []}


def combined_verdict(a_passed: bool, b_verdict: str) -> tuple[str, bool]:
    """分层合并：A 通过 且 judge==pass ⇒ pass，否则 fail。"""
    passed = a_passed and b_verdict == "pass"
    return ("pass" if passed else "fail"), passed


async def judge_one(
    client: Any,
    case: dict[str, Any],
    *,
    source_root: Path,
    wiki_dir: Path,
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """判一条 compile_outcome case，返回 per-layer + combined verdict。

    带 `candidate_pages`（对抗例：内联损坏产出）时，A 层用 score_adversarial_deterministic
    判注入的结构缺陷、B 层对候选语义 judge；不读盘、不跑磁盘 integrity（孤立快照的
    wikilink 会被误判死链）。否则对真实 wiki 产出走 integrity + judge。
    """
    source_path = source_root / case["source"]
    source = source_path.read_text(encoding="utf-8") if source_path.is_file() else ""
    candidate = case.get("candidate_pages")
    if candidate is not None:
        from evals.core.adversarial import score_adversarial_deterministic

        a = score_adversarial_deterministic(case)
        a_passed = not a["caught"]  # A 抓住缺陷 ⇒ 结构层不通过
        pages = [{"path": p["path"], "content": p["content"]} for p in candidate]
        judgement = await judge_case(client, case, source, artifacts or {}, pages)
        judgement["metrics"] = semantic_metrics(judgement)
        verdict, passed = combined_verdict(a_passed, judgement["verdict"])
        return {
            "case_id": case["id"],
            "verdict": verdict,
            "a_passed": a_passed,
            "judge_verdict": judgement["verdict"],
            "combined_passed": passed,
            "metrics": judgement["metrics"],
            "score": judgement,
        }
    pages = [
        {"path": path, "content": (wiki_dir / path).read_text(encoding="utf-8")}
        for path in case.get("expected_pages", [])
        if (wiki_dir / path).is_file()
    ]
    judgement = await judge_case(client, case, source, artifacts or {}, pages)
    judgement["metrics"] = semantic_metrics(judgement)
    judgement["integrity"] = integrity_metrics(
        wiki_dir, pages, allowed_source_identities=case.get("source_identities")
    )
    structure = compile_structure_checks(
        [_structure_page(p) for p in pages],
    )
    judgement["structure"] = structure
    a_passed = bool(judgement["integrity"]["passed"]) and structure["passed"]
    verdict, passed = combined_verdict(a_passed, judgement["verdict"])
    return {
        "case_id": case["id"],
        "verdict": verdict,
        "a_passed": a_passed,
        "judge_verdict": judgement["verdict"],
        "combined_passed": passed,
        "metrics": judgement["metrics"],
        "score": judgement,
    }
