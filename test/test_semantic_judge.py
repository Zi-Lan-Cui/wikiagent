import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.judges.semantic_judge import check_judge_json


def _valid():
    item = {"score": 4, "evidence": ["有证据"], "issues": []}
    return {
        "source_fidelity": item,
        "search_relevance": item,
        "analysis_quality": item,
        "plan_quality": item,
        "page_quality": item,
        "unsupported_claims": [],
        "confidence": 0.8,
        "verdict": "pass",
        "summary": "通过",
    }


def test_judge_schema_accepts_json_fence():
    import json

    ok, reason = check_judge_json("```json\n" + json.dumps(_valid()) + "\n```")
    assert ok, reason


def test_judge_schema_rejects_bad_score():
    data = _valid()
    data["plan_quality"]["score"] = 6
    ok, reason = check_judge_json(__import__("json").dumps(data))
    assert not ok
    assert "score" in reason
