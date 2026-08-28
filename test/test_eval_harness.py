"""Golden eval 元数据和硬约束评分测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.core.harness import GoldenCase, score_summary


def test_manifest_template_has_complete_example():
    import json

    data = json.loads((Path(__file__).parents[1] / "evals/templates/manifest.json").read_text())
    assert data["cases"][0]["cluster"] in {cluster["id"] for cluster in data["clusters"]}


def test_summary_score_accepts_paraphrase_with_required_facts():
    case = GoldenCase(
        "states", "os", "states.md", "", ("创建态", "就绪态", "运行态", "阻塞态", "终止态"), ()
    )
    score = score_summary(
        case,
        "进程可能经历创建态、就绪态、运行态、阻塞态和终止态。",
    )
    assert score.passed
    assert score.coverage == 1.0


def test_summary_score_rejects_missing_fact_or_unsupported_claim():
    case = GoldenCase("order", "cpp", "order.md", "", ("求值顺序",), ("参数按从左到右求值",))
    score = score_summary(case, "参数按从左到右求值，因此不会产生未定义行为。")
    assert not score.passed
    assert "求值顺序" in score.missing
    assert "参数按从左到右求值" in score.forbidden_hits
