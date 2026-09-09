import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.judges.semantic_judge import check_judge_json


def _valid():
    return {
        "grounding": {
            "claims": [
                {
                    "claim": "事实",
                    "verdict": "supported",
                    "evidence": "有证据",
                    "critical": True,
                }
            ]
        },
        "coverage": {"facts": [{"fact_id": "fact-01", "verdict": "covered", "evidence": "有证据"}]},
        "unknown_reasons": [],
        "verdict": "pass",
        "summary": "通过",
    }


def test_judge_schema_accepts_json_fence():
    import json

    ok, reason = check_judge_json("```json\n" + json.dumps(_valid()) + "\n```")
    assert ok, reason


def test_judge_schema_rejects_bad_binary_verdict():
    data = _valid()
    data["grounding"]["claims"][0]["verdict"] = "maybe"
    ok, reason = check_judge_json(__import__("json").dumps(data))
    assert not ok
    assert "verdict" in reason
