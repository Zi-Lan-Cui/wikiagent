"""QA manifest、硬性契约和 Judge JSON schema 测试。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.core.qa_harness import QACase, score_answer, summarize
from evals.judges.qa_semantic_judge import check_qa_judge_json


def test_qa_manifest_covers_core_question_types():
    data = json.loads((Path(__file__).parents[1] / "evals/templates/qa_manifest.json").read_text())
    assert data["version"] == 1
    assert {"id", "type", "question", "must_include", "required_tools"} <= set(data["cases"][0])


def _case(**kwargs):
    defaults = dict(
        type="fact",
        question="q",
        must_include=(),
        must_include_any=(),
        must_not_claim=(),
        must_not_include=(),
        expected_pages=(),
        required_tools=(),
        forbidden_tools=(),
    )
    defaults.update(kwargs)
    return QACase(**defaults)


def test_qa_hard_contract_accepts_grounded_answer():
    case = _case(
        id="states",
        must_include=("创建态", "就绪态", "运行态", "阻塞态"),
        required_tools=("ReadFile",),
    )
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
    case = _case(
        id="thread", must_not_claim=("线程安全保证",), forbidden_tools=("RecordCorrection",)
    )
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
    case = _case(
        id="thread", must_not_claim=("线程安全保证",), forbidden_tools=("RecordCorrection",)
    )
    score = score_answer(
        case,
        {
            "answer": "不能断言 std::thread 本身提供线程安全保证。",
            "tool_calls": [{"name": "ReadFile"}],
        },
    )
    assert score.passed


def test_qa_hard_contract_rejects_explicit_external_knowledge():
    case = _case(
        id="thread",
        must_not_claim=("线程安全保证",),
        must_not_include=("来自我已有知识",),
        forbidden_tools=("RecordCorrection",),
    )
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
    case = _case(
        id="cpu",
        must_include_any=("没有", "未说明"),
        must_not_claim=("具体 CPU 架构的实现细节",),
        required_tools=("ReadFile",),
    )
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
    case = _case(
        id="process",
        must_include=("进程", "程序", "进程控制块", "PCB"),
        required_tools=("ReadFile",),
    )
    score = score_answer(
        case,
        {
            "answer": "进程是程序的一次执行，包含进程控制块 PCB。",
            "tool_calls": [],
        },
    )
    summary = summarize([score])
    assert summary["passed"] == 0
    assert summary["mean_coverage"] == 1.0
