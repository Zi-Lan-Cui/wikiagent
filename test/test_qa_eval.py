"""QA manifest、硬性契约和 Judge JSON schema 测试。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.qa_harness import load_cases, score_answer, summarize
from evals.qa_semantic_judge import check_qa_judge_json


def test_qa_manifest_covers_core_question_types():
    cases = load_cases()
    assert len(cases) >= 10
    assert {case.type for case in cases} >= {
        "fact",
        "compare",
        "relation",
        "uncertainty",
        "followup",
    }


def test_expanded_qa_manifest_has_120_cases_and_five_per_source():
    path = Path(__file__).resolve().parents[1] / "evals/golden/qa_expanded_manifest.json"
    cases = load_cases(path)
    assert len(cases) == 120
    raw_ids = [item["id"] for item in json.loads(path.read_text(encoding="utf-8"))["cases"]]
    assert sum("-cross-" in case_id for case_id in raw_ids) == 20
    assert sum("-cross-" not in case_id for case_id in raw_ids) == 100
    base = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    for item in base["cases"]:
        expected = {
            f"qa-expanded-{item['id']}-{suffix}"
            for suffix in ("facts", "relation", "boundary", "unknown", "scope")
        }
        assert expected <= set(raw_ids)
    assert sum(case.type == "uncertainty" for case in cases) == 40
    assert sum(case.type == "compare" for case in cases) == 20
    assert sum(case.type == "followup" for case in cases) == 20


def test_qa_hard_contract_accepts_grounded_answer():
    case = next(x for x in load_cases() if x.id == "qa-process-states")
    score = score_answer(
        case,
        {
            "answer": "文档列出创建态、就绪态、运行态、阻塞态和终止态。",
            "tool_calls": [{"name": "Grep"}, {"name": "ReadFile"}],
            "citations": ["concepts/process-state.md"],
        },
        existing_pages={"concepts/process-state.md"},
    )
    assert score.passed
    assert score.coverage == 1.0


def test_qa_hard_contract_rejects_side_effect_and_invention():
    case = next(x for x in load_cases() if x.id == "qa-unknown-thread-safety")
    score = score_answer(
        case,
        {
            "answer": "std::thread 提供线程安全保证。",
            "tool_calls": [{"name": "RecordCorrection"}],
        },
    )
    assert not score.passed
    assert "unsupported_or_forbidden_claim" in score.issues
    assert "forbidden_side_effect_tool_used" in score.issues


def test_qa_hard_contract_allows_negated_forbidden_claim():
    case = next(x for x in load_cases() if x.id == "qa-unknown-thread-safety")
    score = score_answer(
        case,
        {
            "answer": "不能断言 std::thread 本身提供线程安全保证。",
            "tool_calls": [{"name": "ReadFile"}],
        },
    )
    assert score.passed


def test_qa_hard_contract_rejects_explicit_external_knowledge():
    case = next(x for x in load_cases() if x.id == "qa-unknown-thread-safety")
    score = score_answer(
        case,
        {
            "answer": "不能从笔记断言。补充：来自我已有知识，mutex 可以保证线程安全。",
            "tool_calls": [{"name": "ReadFile"}],
        },
    )
    assert not score.passed
    assert "external_knowledge_added" in score.issues


def test_qa_hard_contract_accepts_one_of_equivalent_unknown_markers():
    case = next(x for x in load_cases() if x.id == "qa-unknown-cpu")
    score = score_answer(
        case,
        {
            "answer": "没有给出具体 CPU 架构的实现细节。",
            "tool_calls": [{"name": "Grep"}, {"name": "ReadFile"}],
        },
    )
    assert score.passed
    assert score.coverage == 1.0


def test_qa_judge_schema_requires_all_dimensions():
    good = {
        key: {"score": 4, "evidence": [], "issues": []}
        for key in (
            "answer_correctness",
            "answer_completeness",
            "evidence_grounding",
            "uncertainty_handling",
            "cross_page_reasoning",
            "conversation_consistency",
        )
    }
    good.update({"unsupported_claims": [], "confidence": 0.9, "verdict": "pass", "summary": "ok"})
    assert check_qa_judge_json(json.dumps(good))[0]


def test_qa_summary_is_not_semantic_pass():
    case = next(x for x in load_cases() if x.id == "qa-process-components")
    score = score_answer(case, {"answer": "进程控制块 PCB。", "tool_calls": []})
    summary = summarize([score])
    assert summary["passed"] == 0
    assert summary["mean_coverage"] == 1.0
