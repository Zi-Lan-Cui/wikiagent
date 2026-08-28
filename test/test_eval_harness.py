"""Golden eval 元数据和硬约束评分测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.harness import load_cases, score_summary


def test_golden_manifest_has_four_clusters_and_twenty_cases():
    cases = load_cases()
    assert len(cases) == 20
    assert {case.cluster for case in cases} == {
        "operating-system-process",
        "cpp-language-and-runtime",
        "deep-learning-math",
        "agent-architecture",
    }


def test_summary_score_accepts_paraphrase_with_required_facts():
    case = next(case for case in load_cases() if case.id == "os-process-state")
    score = score_summary(
        case,
        "进程可能经历创建态、就绪态、运行态、阻塞态和终止态。",
    )
    assert score.passed
    assert score.coverage == 1.0


def test_summary_score_rejects_missing_fact_or_unsupported_claim():
    case = next(case for case in load_cases() if case.id == "cpp-evaluation-order")
    score = score_summary(case, "参数按从左到右求值，因此不会产生未定义行为。")
    assert not score.passed
    assert "求值顺序" in score.missing
    assert "参数按从左到右求值" in score.forbidden_hits
