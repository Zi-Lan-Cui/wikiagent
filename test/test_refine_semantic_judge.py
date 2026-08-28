import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.refine_semantic_judge import check_refine_judge_json


def _valid():
    item = {"score": 4, "evidence": ["goal 已完成"], "issues": []}
    gap = {**item, "applicable": True}
    return {
        "gap_status": "reducible",
        "goal_completion": item,
        "gap_reduction": gap,
        "boundary_handling": item,
        "source_fidelity": item,
        "scope_discipline": item,
        "relation_quality": item,
        "regression_safety": item,
        "unsupported_claims": [],
        "confidence": 0.8,
        "verdict": "pass",
        "summary": "通过",
    }


def test_refine_judge_schema_accepts_valid_json():
    ok, reason = check_refine_judge_json(json.dumps(_valid()))
    assert ok, reason


def test_refine_judge_schema_rejects_unknown_verdict():
    data = _valid()
    data["verdict"] = "unknown"
    ok, reason = check_refine_judge_json(json.dumps(data))
    assert not ok
    assert "verdict" in reason


def test_refine_judge_accepts_unfillable_gap_without_reduction():
    data = _valid()
    data["gap_status"] = "unsupported"
    data["gap_reduction"] = {
        "score": 5,
        "applicable": False,
        "evidence": ["source 未提供上线日期"],
        "issues": [],
    }
    data["boundary_handling"] = {
        "score": 5,
        "evidence": ["明确说明未提供上线日期"],
        "issues": [],
    }
    ok, reason = check_refine_judge_json(json.dumps(data))
    assert ok, reason


def test_refine_judge_rejects_unknown_gap_status():
    data = _valid()
    data["gap_status"] = "unknown"
    ok, reason = check_refine_judge_json(json.dumps(data))
    assert not ok
    assert "gap_status" in reason
