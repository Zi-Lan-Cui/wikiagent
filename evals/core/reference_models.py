"""评测契约模型：baseline 声明与语料清单、Wiki 快照、评分器定义，以及评测记录。

评分器定义 evals/rubrics/stage-rubrics-v1.json 是评测标准的唯一来源、跨 source
通用，评测记录是运行产物——两层各用各的模型，互不混写。评判模型的校准断言从
run-evidence 现抽、不入库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BaselineSpec(BaseModel):
    id: str
    wiki_path: str
    wiki_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_path: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    description: str

    @model_validator(mode="after")
    def source_declaration_is_complete(self) -> BaselineSpec:
        """source 声明必须成对出现——只给一半是笔误。"""
        if (self.source_path is None) != (self.source_sha256 is None):
            raise ValueError("source_path 与 source_sha256 必须同时声明")
        return self


class CorpusManifest(BaseModel):
    schema_version: Literal[2]
    corpus_id: str
    description: str
    baselines: list[BaselineSpec]

    @model_validator(mode="after")
    def baseline_ids_are_unique(self) -> CorpusManifest:
        ids = [baseline.id for baseline in self.baselines]
        if len(ids) != len(set(ids)):
            raise ValueError("baseline id 必须唯一")
        return self


class WikiFileEntry(BaseModel):
    """baseline 或 candidate Wiki 快照中可见的单个文件。"""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["page", "index", "schema", "purpose", "asset", "other"]
    title: str = ""
    summary: str = ""
    headings: list[str] = Field(default_factory=list)


class WikiSnapshot(BaseModel):
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[WikiFileEntry]

    @model_validator(mode="after")
    def paths_are_unique(self) -> WikiSnapshot:
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("Wiki snapshot 中的文件路径必须唯一")
        return self


# ── 评分器定义（通用层，对应 evals/rubrics/stage-rubrics-v1.json）──────────

StageName = Literal["extract", "analyze", "plan", "search", "execute"]
FiveLevelBand = Literal["1", "2", "3", "4", "5"]


class RubricDimension(BaseModel):
    """一个阶段里的一条评分维度——人写，跨 source 复用。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Z]\d$")
    name: str = Field(min_length=2)
    weight: float = Field(gt=0, le=1)
    criteria: str = Field(min_length=1)
    scoring: dict[FiveLevelBand, str]

    @model_validator(mode="after")
    def scoring_has_all_five_levels(self) -> RubricDimension:
        if set(self.scoring) != {"1", "2", "3", "4", "5"}:
            raise ValueError(f"维度 {self.id} 缺少分档描述：{sorted(self.scoring)}")
        return self


class StageRubric(BaseModel):
    """一个阶段的完整 rubric——维度权重和必须为 1.0。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    artifact_kind: str
    visible_inputs_rule: str
    dimensions: list[RubricDimension] = Field(min_length=1)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> StageRubric:
        total = sum(d.weight for d in self.dimensions)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"维度权重和必须为 1.0，实际 {total}")
        ids = [d.id for d in self.dimensions]
        if len(ids) != len(set(ids)):
            raise ValueError("维度 id 必须唯一")
        return self

    def weight_of(self, dimension_id: str) -> float:
        for dimension in self.dimensions:
            if dimension.id == dimension_id:
                return dimension.weight
        raise ValueError(f"未知维度 id: {dimension_id}")


class DeterministicCheckRule(BaseModel):
    """确定性校验规则——只描述规则，不含结果。结果属于评测记录。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    kind: Literal["range", "regex", "unique", "enum", "schema", "formula"]
    expr: str
    gate: bool = True
    pointer: str = ""


class GraderDefinition(BaseModel):
    """评分器定义——通用层，不含任何 source、产物路径或分数。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["wiki-agent-grader-v1"]
    stage_rubrics: dict[str, StageRubric]
    deterministic_checks: dict[str, list[DeterministicCheckRule]]
    aggregation: dict

    @model_validator(mode="after")
    def covers_all_five_stages(self) -> GraderDefinition:
        required = {"extract", "analyze", "plan", "search", "execute"}
        if set(self.stage_rubrics) != required:
            raise ValueError(f"stage_rubrics 必须覆盖五阶段，缺 {required - set(self.stage_rubrics)}")
        # 校验段只许描述真实阶段，防止 S-EMPTY 写成 s-EMPTY 静默不生效
        if not set(self.deterministic_checks) <= required:
            raise ValueError(
                f"deterministic_checks 含未知阶段: {set(self.deterministic_checks) - required}"
            )
        return self


def load_grader(path: Path) -> GraderDefinition:
    """读评分器定义——解析失败即抛，调用方不得带着不完整的定义继续跑。"""
    return GraderDefinition.model_validate_json(path.read_text(encoding="utf-8"))


# ── 评测记录（输出层，落在各语料目录的 runs/）───────────────────────────────


class CheckResult(BaseModel):
    """一次确定性校验的结果——由程序产生。gate 随结果落盘，
    否则记录单独回读时无法分辨这条规则拦不拦。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    passed: bool
    gate: bool = True
    detail: str = ""


class DimensionScore(BaseModel):
    """一个维度的得分——由模型或人产生。provenance 区分实测/人标/占位，
    没有它就无法分辨 `"score": 4` 是真判还是示意。"""

    model_config = ConfigDict(extra="forbid")

    dimension_id: str
    score: int | None = Field(default=None, ge=1, le=5)
    provenance: Literal["judge", "human", "demo"] = "judge"
    rationale: str = ""


class StageEvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: StageName
    artifact_path: str
    artifact_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    # judged 与故障/缺席分开报，汇报「哪些阶段缺席」才不含混
    status: Literal["scored", "missing_artifact", "judge_failed"]
    deterministic: list[CheckResult]
    dimensions: list[DimensionScore]
    stage_score: float | None = None


class SourceEvalRecord(BaseModel):
    """输出层——某一次运行对某篇 source 的完整评测结果。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["wiki-agent-source-eval-v1"]
    run_id: str
    source: str
    rubric_version: str
    judge_model: str = ""
    stages: list[StageEvalRecord]
    source_score: float | None = None
    gate: dict
    # gate 校准（抽样人标）完成前只出分不判：incomplete 表示结论未生效
    verdict: Literal["pass", "fail", "incomplete"]
