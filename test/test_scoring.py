"""聚合与门槛（C 组验收）——纯函数，构造输入即可判定。

直接运行:  .venv/bin/python test/test_scoring.py
"""

import pytest

from evals.core.reference_models import (
    CheckResult,
    DimensionScore,
    SourceEvalRecord,
    StageEvalRecord,
    StageRubric,
)
from evals.core.scoring import evaluate_gate, source_score, stage_score

DEFAULT_STAGE_WEIGHTS = {
    "extract": 0.15,
    "analyze": 0.15,
    "plan": 0.25,
    "search": 0.05,
    "execute": 0.4,
}


def _rubric(weights: list[float]) -> StageRubric:
    return StageRubric.model_validate(
        {
            "seq": 1,
            "artifact_kind": "test",
            "visible_inputs_rule": "测试",
            "dimensions": [
                {
                    "id": f"T{i + 1}",
                    "name": f"维度{i + 1}",
                    "weight": weight,
                    "criteria": "测试判据",
                    "scoring": {str(level): "描述" for level in range(1, 6)},
                }
                for i, weight in enumerate(weights)
            ],
        }
    )


def test_c1_weighted_stage_score():
    rubric = _rubric([0.35, 0.30, 0.20, 0.15])
    dimensions = [
        DimensionScore(dimension_id=f"T{i + 1}", score=score)
        for i, score in enumerate([4, 3, 2, 5])
    ]
    assert stage_score(dimensions, rubric) == 3.45


def test_c2_unscored_dimension_leaves_denominator():
    rubric = _rubric([0.35, 0.30, 0.20, 0.15])
    dimensions = [
        DimensionScore(dimension_id="T1", score=4),
        DimensionScore(dimension_id="T2", score=3),
        DimensionScore(dimension_id="T3", score=None),
        DimensionScore(dimension_id="T4", score=5),
    ]
    assert stage_score(dimensions, rubric) == round(3.05 / 0.8, 4)


def test_c2b_unknown_dimension_id_raises():
    rubric = _rubric([1.0])
    with pytest.raises(ValueError, match="未知维度"):
        stage_score([DimensionScore(dimension_id="T9", score=3)], rubric)


def test_c3_source_score_and_c4_missing_stage_excluded():
    full = {
        "extract": 3.45,
        "analyze": 4.25,
        "plan": 4.20,
        "search": 5.00,
        "execute": 3.85,
    }
    assert source_score(full, DEFAULT_STAGE_WEIGHTS) == 3.995
    with_missing = {**full, "search": None}
    # 缺席阶段权重从分母剔除：分母 0.95，分数不因「没测」被稀释
    assert source_score(with_missing, DEFAULT_STAGE_WEIGHTS) == round(
        (3.45 * 0.15 + 4.25 * 0.15 + 4.20 * 0.25 + 3.85 * 0.4) / 0.95, 4
    )


def _record(dimension_scores: list[int | None], source: float | None, det_pass: bool = True) -> SourceEvalRecord:
    stages = [
        StageEvalRecord(
            stage="plan",
            artifact_path="plan.json",
            status="scored",
            deterministic=[CheckResult(rule_id="P-PATH", passed=det_pass, gate=True)],
            dimensions=[
                DimensionScore(dimension_id=f"T{i + 1}", score=score)
                for i, score in enumerate(dimension_scores)
            ],
        )
    ]
    return SourceEvalRecord(
        schema_version="wiki-agent-source-eval-v1",
        run_id="test",
        source="t.md",
        rubric_version="v1",
        stages=stages,
        source_score=source,
        gate={},
        verdict="incomplete",
    )


def test_c5_min_dimension_one_blocks_gate():
    gate = evaluate_gate(_record([4, 3, 1, 5], 4.0), threshold=3.0, min_dim=3)
    assert gate["passed"] is False and gate["min_dimension_ok"] is False


def test_c6_all_dims_ok_but_score_below_threshold():
    # 维度均踩达标线、总分却因阶段加权落在阈值下——单验 score_ok 这一支
    gate = evaluate_gate(_record([3, 3, 3, 3], 2.5), threshold=3.0, min_dim=3)
    assert gate["passed"] is False and gate["min_dimension_ok"] and not gate["score_ok"]


def test_c7_gate_uses_checkresult_gate_bit_not_rule_table():
    """gate=false 的规则不拦——CheckResult 自带 gate 位，记录可单独回读审计。"""
    record = _record([4, 3, 3, 4], 4.0)
    record.stages[0].deterministic = [
        CheckResult(rule_id="E-LEN", passed=False, gate=False)
    ]
    assert evaluate_gate(record, threshold=3.0, min_dim=3)["passed"] is True


def test_c8_no_scored_dimensions_blocks_gate():
    assert evaluate_gate(_record([None], None), threshold=3.0, min_dim=3)["passed"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
