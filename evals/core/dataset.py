"""v3 评测集 schema + 确定性(A 层)评分器 + 参考解自检。

单一职责：把"任务定义"与"确定性门禁"收敛一处，供 build_dataset/label/agreement/
snapshot 与回归测试共用。A 层不碰 LLM，可在 CI 无凭证跑。

case schema v3（在 v2 基础上新增）：
    suite:        "capability" | "regression"
    polarity:     "positive" | "negative"
    expected_behavior: {allow_pages: bool, require_noop: bool}
    reference_solution: 证明任务可解 + 评分器配置正确的参考产物
    human_verdict / reviewer: 人工金标（Layer C，Phase 1 签核后填）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.core.outcomes import integrity_metrics

SUITES = {"capability", "regression"}
POLARITIES = {"positive", "negative"}
VERDICTS = {"pass", "fail", "review"}


def validate_schema_v3(dataset: dict[str, Any]) -> list[str]:
    """返回数据集结构性错误列表（空=合法）。"""
    errors: list[str] = []
    if dataset.get("version") != 3:
        errors.append(f"version 必须为 3，实际 {dataset.get('version')!r}")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        return [*errors, "cases 必须是非空列表"]
    seen_ids: set[str] = set()
    for case in cases:
        cid = str(case.get("id", "<no-id>"))
        if cid in seen_ids:
            errors.append(f"{cid}: id 重复")
        seen_ids.add(cid)
        for field in ("task_type", "polarity", "suite", "expected_verdict"):
            if field not in case:
                errors.append(f"{cid}: 缺字段 {field}")
        if case.get("polarity") not in POLARITIES:
            errors.append(f"{cid}: polarity 非法 {case.get('polarity')!r}")
        if case.get("suite") not in SUITES:
            errors.append(f"{cid}: suite 非法 {case.get('suite')!r}")
        if case.get("expected_verdict") not in VERDICTS:
            errors.append(f"{cid}: expected_verdict 非法")
        if "reference_solution" not in case:
            errors.append(f"{cid}: 缺 reference_solution（任务须自带可解证明）")
    return errors


def score_refusal(
    case: dict[str, Any],
    *,
    produced_paths: set[str],
    rendered_text: str,
) -> dict[str, Any]:
    """负例（应拒答/应 no-op）确定性评分。

    通过条件：case 要求 no-op、未产出任何（预期）页、未命中任何 forbidden_claim。
    编造被禁止的页/论断 => 不通过。这是"不该下结论时不臆造"的门禁。
    """
    expected_behavior = case.get("expected_behavior", {})
    require_noop = bool(expected_behavior.get("require_noop"))
    forbidden = [str(f) for f in case.get("forbidden_claims", [])]
    forbidden_hits = [f for f in forbidden if f and f in rendered_text]
    invented_pages = set(produced_paths) - set(case.get("expected_pages", []))
    passed = require_noop and not produced_paths and not forbidden_hits and not invented_pages
    return {
        "case_id": case.get("id", ""),
        "passed": passed,
        "require_noop": require_noop,
        "produced_pages": sorted(produced_paths),
        "invented_pages": sorted(invented_pages),
        "forbidden_hits": forbidden_hits,
    }


def _reference_text(case: dict[str, Any]) -> str:
    """参考解正文：正例取逐字 required_facts（必然满足 must_include、无 forbidden）。"""
    facts = [str(f.get("assertion", "")) for f in case.get("required_facts", [])]
    return "\n".join(facts)


def run_reference_graders(case: dict[str, Any], wiki_dir: Path) -> dict[str, Any]:
    """对单条 case 的参考解跑全部 A 层评分器。

    正例：expected_pages 必须存在+有指定来源 provenance（integrity），
          且 required_facts 全部出现在参考正文（must_include 无 missing）、
          forbidden_claims 不出现。
    负例：参考解=no-op/abstain，score_refusal 在"无产出页 + 无 forbidden 命中"下应通过。
    返回 {passed, checks}；passed=False 表示任务定义或评分器配置有问题。
    """
    polarity = case.get("polarity")
    checks: dict[str, Any] = {}
    if polarity == "negative":
        refusal = score_refusal(case, produced_paths=set(), rendered_text="")
        checks["refusal"] = refusal
        return {"case_id": case.get("id"), "passed": refusal["passed"], "checks": checks}

    pages = [
        {"path": p, "content": ""}
        for p in case.get("expected_pages", [])
    ]
    integrity = integrity_metrics(
        wiki_dir, pages, allowed_source_identities=case.get("source_identities")
    )
    text = _reference_text(case)
    facts = [str(f.get("assertion", "")) for f in case.get("required_facts", [])]
    missing = [f for f in facts if f not in text]
    forbidden = [str(f) for f in case.get("forbidden_claims", [])]
    forbidden_hits = [f for f in forbidden if f and f in text]
    checks["integrity"] = integrity
    checks["must_include_missing"] = missing
    checks["forbidden_hits"] = forbidden_hits
    passed = integrity["passed"] and not missing and not forbidden_hits
    return {"case_id": case.get("id"), "passed": passed, "checks": checks}


def assert_all_references_pass(dataset: dict[str, Any], wiki_dir: Path) -> list[str]:
    """所有 case 的参考解必须过 A 层——否则任务不可解或评分器配错。"""
    failures: list[str] = []
    for case in dataset.get("cases", []):
        result = run_reference_graders(case, Path(wiki_dir))
        if not result["passed"]:
            failures.append(f"{result['case_id']}: 参考解未过 A 层 -> {_why(result)}")
    return failures


def _why(result: dict[str, Any]) -> str:
    checks = result.get("checks", {})
    integrity = checks.get("integrity")
    bits: list[str] = []
    if integrity and not integrity.get("passed"):
        if integrity.get("errors"):
            bits.append("integrity:" + ";".join(e["reason"] for e in integrity["errors"][:2]))
        if integrity.get("missing_provenance"):
            bits.append("provenance缺失")
    if checks.get("must_include_missing"):
        bits.append("缺事实")
    if checks.get("forbidden_hits"):
        bits.append("命中禁语")
    refusal = checks.get("refusal")
    if refusal and not refusal.get("passed"):
        bits.append("负例参考解不自洽")
    return " ".join(bits) or "unknown"
