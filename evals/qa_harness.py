"""QA 评测的确定性契约层。

输入是一次或多次 Agent 回答的记录，不负责调用模型。语义正确性由
qa_semantic_judge 评估；这里负责发现明显的协议、证据和副作用错误。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST = Path(__file__).parent / "golden" / "qa_manifest.json"


@dataclass(frozen=True)
class QACase:
    id: str
    type: str
    question: str
    must_include: tuple[str, ...]
    must_include_any: tuple[str, ...]
    must_not_claim: tuple[str, ...]
    must_not_include: tuple[str, ...]
    expected_pages: tuple[str, ...]
    required_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    conversation: tuple[str, ...] = ()


@dataclass(frozen=True)
class QAScore:
    case_id: str
    passed: bool
    answer_present: bool
    coverage: float
    missing: tuple[str, ...]
    forbidden_hits: tuple[str, ...]
    missing_tools: tuple[str, ...]
    forbidden_tools_used: tuple[str, ...]
    missing_citations: tuple[str, ...]
    side_effect_free: bool
    issues: tuple[str, ...]


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(path: Path = DEFAULT_MANIFEST) -> list[QACase]:
    return [
        QACase(
            id=item["id"],
            type=item["type"],
            question=item["question"],
            must_include=tuple(item.get("must_include", [])),
            must_include_any=tuple(item.get("must_include_any", [])),
            must_not_claim=tuple(item.get("must_not_claim", [])),
            must_not_include=tuple(item.get("must_not_include", [])),
            expected_pages=tuple(item.get("expected_pages", [])),
            required_tools=tuple(item.get("required_tools", [])),
            forbidden_tools=tuple(item.get("forbidden_tools", [])),
            conversation=tuple(item.get("conversation", [])),
        )
        for item in load_manifest(path)["cases"]
    ]


def _unsupported_assertion(answer: str, phrase: str) -> bool:
    """命中禁止主张，但允许“没有/不能/未说明该主张”的否定句。"""
    for match in re.finditer(re.escape(phrase), answer, flags=re.IGNORECASE):
        prefix = answer[max(0, match.start() - 24) : match.start()]
        prefix = re.split(r"[。！？!?；;\n]", prefix)[-1]
        if not re.search(r"(?:不|未|没有|无|不能|无法|并非|不应|尚未|是否|能否|可否)", prefix):
            return True
    return False


def score_answer(
    case: QACase, answer_record: dict[str, Any], *, existing_pages: set[str] | None = None
) -> QAScore:
    answer = str(answer_record.get("answer", "") or "")
    calls = answer_record.get("tool_calls", []) or []
    tool_names = {str(item.get("name", "")) for item in calls if isinstance(item, dict)}
    citations = {str(item) for item in answer_record.get("citations", []) or []}
    missing = tuple(x for x in case.must_include if x not in answer)
    any_hit = any(x in answer for x in case.must_include_any)
    missing_any = (
        (" | ".join(case.must_include_any),) if case.must_include_any and not any_hit else ()
    )
    forbidden_hits = tuple(x for x in case.must_not_claim if _unsupported_assertion(answer, x))
    external_hits = tuple(x for x in case.must_not_include if x.lower() in answer.lower())
    missing_tools = tuple(x for x in case.required_tools if x not in tool_names)
    forbidden_used = tuple(x for x in case.forbidden_tools if x in tool_names)
    missing_citations = ()
    if existing_pages is not None:
        missing_citations = tuple(x for x in citations if x not in existing_pages)
    answer_present = bool(answer.strip())
    side_effect_free = not forbidden_used
    issues: list[str] = []
    if not answer_present:
        issues.append("answer_empty")
    if missing or missing_any:
        issues.append("missing_required_facts")
    if forbidden_hits:
        issues.append("unsupported_or_forbidden_claim")
    if external_hits:
        issues.append("external_knowledge_added")
    if missing_tools:
        issues.append("required_tool_not_used")
    if forbidden_used:
        issues.append("forbidden_side_effect_tool_used")
    if missing_citations:
        issues.append("citation_page_not_found")
    total = len(case.must_include) + (1 if case.must_include_any else 0)
    covered = total - len(missing) - len(missing_any)
    coverage = covered / total if total else 1.0
    passed = not issues
    return QAScore(
        case.id,
        passed,
        answer_present,
        coverage,
        missing,
        forbidden_hits + external_hits,
        missing_tools,
        forbidden_used,
        missing_citations,
        side_effect_free,
        tuple(issues),
    )


def summarize(scores: list[QAScore]) -> dict[str, Any]:
    return {
        "cases": len(scores),
        "passed": sum(x.passed for x in scores),
        "pass_rate": sum(x.passed for x in scores) / len(scores) if scores else 0.0,
        "answer_present": sum(x.answer_present for x in scores),
        "mean_coverage": sum(x.coverage for x in scores) / len(scores) if scores else 0.0,
        "side_effect_free": sum(x.side_effect_free for x in scores),
    }
