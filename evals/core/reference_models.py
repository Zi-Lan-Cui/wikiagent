"""Contracts for seeded-baseline corpora, run evidence, and judgement cases."""

from __future__ import annotations

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
    """One file visible in a baseline or candidate Wiki snapshot."""

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


class JudgementCase(BaseModel):
    """One binary judgement question (v2 format).

    View 记录编译期视角——compiler 编译该 source 那一刻真实可见的
    文件清单，判定纪律见 evals/corpora/reference-v1/verdicts/README.md。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    dimension: Literal["grounding", "coverage", "organization", "uncertainty"]
    stage: Literal["extract", "plan", "execute"]
    source: str
    target: dict
    view: dict
    candidate: str | None = None
    claim: str = Field(min_length=1)
    gold: bool
    evidence: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class JudgementSet(BaseModel):
    schema_version: Literal[2]
    set_id: str
    description: str
    cases: list[JudgementCase]

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> JudgementSet:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case id 必须唯一")
        return self
