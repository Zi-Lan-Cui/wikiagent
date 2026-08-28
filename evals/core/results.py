"""跨组件评测结果和人工真值统计。"""

from __future__ import annotations

from collections import Counter
from typing import Any

VERDICTS = {"pass", "fail", "review"}


def _verdict(item: dict[str, Any]) -> str | None:
    value = item.get("verdict", item.get("status"))
    return value if value in VERDICTS else None


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    actual = Counter(_verdict(case) for case in cases)
    actual.pop(None, None)
    labeled = [case for case in cases if case.get("expected_verdict") in VERDICTS]
    matrix = Counter(
        (case["expected_verdict"], _verdict(case)) for case in labeled if _verdict(case) is not None
    )
    correct = sum(value for (expected, predicted), value in matrix.items() if expected == predicted)
    precision = correct / sum(matrix.values()) if matrix else None
    recall = correct / len(labeled) if labeled else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None
    return {
        "cases": len(cases),
        "actual": dict(actual),
        "labeled_cases": len(labeled),
        "confusion_matrix": {f"{e}->{p}": n for (e, p), n in matrix.items()},
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def aggregate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    all_cases: list[dict[str, Any]] = []
    components: dict[str, dict[str, Any]] = {}
    for report in reports:
        component = str(report.get("component", "unknown"))
        cases = report.get("cases", [])
        if isinstance(cases, list):
            cases = [case for case in cases if isinstance(case, dict)]
        else:
            cases = []
        summary = summarize_cases(cases)
        components[component] = summary
        all_cases.extend({"component": component, **case} for case in cases)
    total = summarize_cases(all_cases)
    status = (
        "fail"
        if total["actual"].get("fail", 0)
        else ("review" if total["actual"].get("review", 0) else "pass")
    )
    return {"status": status, "components": components, "total": total}
