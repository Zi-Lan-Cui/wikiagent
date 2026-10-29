"""评分聚合与门槛判定"""

from __future__ import annotations

from collections.abc import Mapping

from evals.core.reference_models import DimensionScore, SourceEvalRecord, StageRubric


def stage_score(dimensions: list[DimensionScore], rubric: StageRubric) -> float | None:
    """阶段内聚合：Σ(维度分 × 维度权重) / Σ(已评维度权重)。"""
    numerator = denominator = 0.0
    for item in dimensions:
        if item.score is None:
            continue
        weight = rubric.weight_of(item.dimension_id)
        numerator += item.score * weight
        denominator += weight
    return round(numerator / denominator, 4) if denominator else None


def source_score(
    stage_scores: Mapping[str, float | None], stage_weights: Mapping[str, float]
) -> float | None:
    """跨阶段聚合：Σ(阶段分 × 阶段权重) / Σ(已评阶段权重)。"""
    numerator = denominator = 0.0
    for stage, score in stage_scores.items():
        if score is None:
            continue
        weight = stage_weights.get(stage)
        if weight is None:
            raise ValueError(f"未知阶段权重: {stage!r}")
        numerator += score * weight
        denominator += weight
    return round(numerator / denominator, 4) if denominator else None


def evaluate_gate(
    record: SourceEvalRecord, *, threshold: float, min_dim: int
) -> dict:
    """门槛判定：确定性校验通过，且最低维度分 ≥ min_dim，且总分 ≥ threshold。
    """
    deterministic_ok = all(
        result.passed
        for stage_record in record.stages
        for result in stage_record.deterministic
        if result.gate
    )
    scored = [
        dimension.score
        for stage_record in record.stages
        for dimension in stage_record.dimensions
        if dimension.score is not None
    ]
    min_dimension = min(scored) if scored else None
    min_dimension_ok = min_dimension is not None and min_dimension >= min_dim
    score_ok = record.source_score is not None and record.source_score >= threshold
    return {
        "deterministic_all_pass": deterministic_ok,
        "min_dimension": min_dimension,
        "min_dimension_ok": min_dimension_ok,
        "score": record.source_score,
        "score_ok": score_ok,
        "passed": deterministic_ok and min_dimension_ok and score_ok,
    }
