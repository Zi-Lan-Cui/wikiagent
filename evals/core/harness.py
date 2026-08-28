"""第一版离线 eval harness。

本模块先负责黄金集完整性和摘要硬约束评分，不调用 LLM。
真实 LLM runner 可以在此基础上接入现有 Extractor/CompilePipeline，
避免把“样本定义”和“模型调用”混在一起。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "templates" / "manifest.json"


@dataclass(frozen=True)
class GoldenCase:
    id: str
    cluster: str
    source: str
    sha256: str
    must_include: tuple[str, ...]
    must_not_invent: tuple[str, ...]


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    passed: bool
    coverage: float
    missing: tuple[str, ...]
    forbidden_hits: tuple[str, ...]


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(path: Path = DEFAULT_MANIFEST) -> list[GoldenCase]:
    data = load_manifest(path)
    return [
        GoldenCase(
            id=item["id"],
            cluster=item["cluster"],
            source=item["source"],
            sha256=item["sha256"],
            must_include=tuple(item.get("must_include", [])),
            must_not_invent=tuple(item.get("must_not_invent", [])),
        )
        for item in data["cases"]
    ]


def notebook_root(root: Path | None = None) -> Path:
    value = root or os.getenv("WIKI_NOTEBOOK_ROOT")
    if not value:
        raise RuntimeError("请设置 WIKI_NOTEBOOK_ROOT 指向笔记根目录")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"笔记根目录不存在: {path}")
    return path


def source_path(case: GoldenCase, root: Path | None = None) -> Path:
    path = notebook_root(root) / case.source
    if not path.is_file():
        raise FileNotFoundError(f"黄金样本 source 不存在: {path}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sources(root: Path | None = None) -> list[str]:
    """验证所有样本仍指向被人工审阅过的原文。"""
    errors: list[str] = []
    for case in load_cases():
        try:
            path = source_path(case, root)
            actual = sha256_file(path)
        except (FileNotFoundError, RuntimeError) as exc:
            errors.append(str(exc))
            continue
        if actual != case.sha256:
            errors.append(
                f"{case.id}: source 已变化，expected={case.sha256[:12]} actual={actual[:12]}"
            )
    return errors


def score_summary(case: GoldenCase, summary: str) -> CaseScore:
    """只做硬约束评分；同义改写不因文本不一致而失败。"""
    missing = tuple(fact for fact in case.must_include if fact not in summary)
    forbidden_hits = tuple(fact for fact in case.must_not_invent if fact in summary)
    total = len(case.must_include)
    coverage = (total - len(missing)) / total if total else 1.0
    return CaseScore(
        case_id=case.id,
        passed=not missing and not forbidden_hits,
        coverage=coverage,
        missing=missing,
        forbidden_hits=forbidden_hits,
    )


def manifest_summary(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    data = load_manifest(path)
    clusters = data["clusters"]
    cases = data["cases"]
    return {
        "version": data["version"],
        "clusters": len(clusters),
        "cases": len(cases),
        "case_ids": [case["id"] for case in cases],
    }
