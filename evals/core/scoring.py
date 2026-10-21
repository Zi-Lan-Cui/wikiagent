"""聚合与门槛——纯函数，不调 LLM、不读文件。

两个归一条款是本模块的立身之本：未评维度不进分母、未评阶段不进分母——
否则缺席阶段会把总分拉向 0，分数就偏离「已评部分做得好不好」。
代价是总分不跨 run 可比（分母不同），所以评测记录必须带 status、
汇报时必须写明哪几个阶段缺席。
"""

from __future__ import annotations

from collections.abc import Mapping

from evals.core.reference_models import DimensionScore, SourceEvalRecord, StageRubric


def stage_score(dimensions: list[DimensionScore], rubric: StageRubric) -> float | None:
    """阶段内聚合：Σ(维度分 × 维度权重) / Σ(已评维度权重)。

    dimension_id 不在 rubric 里直接抛 ValueError——维度名写错若静默丢分，
    等于给评分器开了个无人察觉的旁路。
    """
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
    """门槛判定：全部 gate=true 确定性校验通过 ∧ 已评维度全部 ≥min_dim ∧
    source_score ≥threshold。threshold 与 min_dim 由调用方按评分器定义传入、
    本函数不内置，免得阈值出现两份来源而各自漂移。

    gate 位随 CheckResult 落盘，因此本函数只读记录、不需要评分器定义。
    量表未经抽样人标校准时 passed 只是形式结论，调用方据此记 verdict=incomplete。
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
