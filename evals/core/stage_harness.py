"""Search/Analyze/Plan 阶段的离线评测。

这里评测的是阶段契约和可追溯性，不把 LLM 的自由文本当作精确字符串
答案。语义相关性另由可选 judge 评测；本模块先保证阶段没有产生幽灵引用、
非法决策或无法供下游消费的结构。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evals.core.harness import GoldenCase, load_cases, score_summary

_VALID_DIRS = ("concepts/", "entities/", "topics/")
_VALID_RELATIONS = {"duplicate", "extends", "related", "contradicts", "unrelated"}
_VALID_DISPOSITIONS = {"new", "update"}


@dataclass
class StageScore:
    stage: str
    passed: bool
    checks: dict[str, bool]
    metrics: dict[str, int | float]
    failures: list[str]


@dataclass
class CaseStageScore:
    case_id: str
    source: str
    summary: Any
    search: StageScore
    analyze: StageScore
    plan: StageScore


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _valid_wiki_path(path: str) -> bool:
    return path.endswith(".md") and path.startswith(_VALID_DIRS)


def _page_exists(wiki_dir: Path, path: str) -> bool:
    return (wiki_dir / path.removeprefix("wiki/")).is_file()


def _ref_key(path: str) -> str:
    """将关系引用归一为不带 .md、无 wiki 前缀的 slug。"""
    return path.removeprefix("wiki/").removesuffix(".md")


def _score_search(data: dict[str, Any], wiki_dir: Path) -> StageScore:
    paths = data.get("rel_paths", [])
    failures: list[str] = []
    checks = {
        "artifact_present": not data.get("_missing", False),
        "array": isinstance(paths, list),
        "valid_paths": all(isinstance(p, str) and _valid_wiki_path(p) for p in paths),
        "unique": len(paths) == len(set(paths)),
        "no_ghost_pages": all(_page_exists(wiki_dir, p) for p in paths),
    }
    if not checks["array"]:
        failures.append("rel_paths 不是数组")
        paths = []
    for name, ok in checks.items():
        if not ok:
            failures.append(
                {
                    "artifact_present": "缺少 search.json 阶段产物",
                    "valid_paths": "包含非法目录或非 Markdown 路径",
                    "unique": "候选页面重复",
                    "no_ghost_pages": "候选页面在当前 Wiki 中不存在",
                }.get(name, name)
            )
    return StageScore(
        stage="search",
        passed=not failures,
        checks=checks,
        metrics={"candidate_count": len(paths)},
        failures=failures,
    )


def _score_analyze(data: dict[str, Any], search: dict[str, Any]) -> StageScore:
    relationships = data.get("relationships", [])
    candidates = {_ref_key(p) for p in search.get("rel_paths", [])}
    source = data.get("source", "")
    failures: list[str] = []
    checks = {
        "artifact_present": not data.get("_missing", False),
        "object": isinstance(data, dict),
        "raw_present_when_needed": bool(data.get("raw")) or not candidates,
        "valid_relationships": isinstance(relationships, list),
        "relationship_refs": True,
        "known_relation_types": True,
    }
    if not isinstance(relationships, list):
        relationships = []
    for rel in relationships:
        if not isinstance(rel, dict):
            checks["relationship_refs"] = False
            checks["known_relation_types"] = False
            continue
        refs = {_ref_key(rel.get("from_page", "")), _ref_key(rel.get("to_page", ""))}
        allowed = candidates | {_ref_key(source), "current-doc"}
        if not refs <= allowed:
            checks["relationship_refs"] = False
        if rel.get("relation") not in _VALID_RELATIONS:
            checks["known_relation_types"] = False
    if not checks["raw_present_when_needed"]:
        failures.append("有 search 候选但没有 analyze 原始输出")
    if not checks["artifact_present"]:
        failures.append("缺少 analyze.json 阶段产物")
    if not checks["valid_relationships"]:
        failures.append("relationships 不是数组")
    if not checks["relationship_refs"]:
        failures.append("关系引用了 search 候选之外的页面")
    if not checks["known_relation_types"]:
        failures.append("存在未知关系类型")
    return StageScore(
        stage="analyze",
        passed=not failures,
        checks=checks,
        metrics={
            "entity_count": len(data.get("entities", [])),
            "concept_count": len(data.get("concepts", [])),
            "relationship_count": len(relationships),
        },
        failures=failures,
    )


def _score_plan(data: dict[str, Any], wiki_dir: Path) -> StageScore:
    targets = data.get("page_targets", [])
    failures: list[str] = []
    paths = [t.get("wiki_path") for t in targets if isinstance(t, dict)]
    checks = {
        "artifact_present": not data.get("_missing", False),
        "array": isinstance(targets, list),
        "valid_paths": all(isinstance(p, str) and _valid_wiki_path(p) for p in paths),
        "unique_paths": len(paths) == len(set(paths)),
        "valid_dispositions": all(
            isinstance(t, dict) and t.get("disposition") in _VALID_DISPOSITIONS for t in targets
        ),
        "has_decision_context": all(
            isinstance(t, dict)
            and bool(str(t.get("title", "")).strip())
            and bool(str(t.get("reason", "")).strip())
            for t in targets
        ),
    }
    for name, ok in checks.items():
        if not ok:
            failures.append(
                {
                    "artifact_present": "缺少 plan.json 阶段产物",
                    "valid_paths": "plan 含非法页面路径",
                    "unique_paths": "plan 含重复页面目标",
                    "valid_dispositions": "plan 含未知 disposition",
                    "has_decision_context": "页面目标缺少 title 或 reason",
                }.get(name, name)
            )
    return StageScore(
        stage="plan",
        passed=not failures,
        checks=checks,
        metrics={
            "target_count": len(targets) if isinstance(targets, list) else 0,
            "new_count": sum(t.get("disposition") == "new" for t in targets if isinstance(t, dict)),
            "update_count": sum(
                t.get("disposition") == "update" for t in targets if isinstance(t, dict)
            ),
            "existing_target_count": sum(
                _page_exists(wiki_dir, p) for p in paths if isinstance(p, str)
            ),
        },
        failures=failures,
    )


def score_run(
    run_dir: Path, wiki_dir: Path, cases: list[GoldenCase] | None = None
) -> list[CaseStageScore]:
    """评分一次 compile run 的 search/analyze/plan 产物。"""
    artifacts = run_dir / "artifacts"
    results: list[CaseStageScore] = []
    for case in cases or load_cases():
        folder = artifacts / Path(case.source).name
        extract = (
            (folder / "extract.json").read_text(encoding="utf-8")
            if (folder / "extract.json").exists()
            else ""
        )
        search = _load_json(folder / "search.json", {"_missing": True, "rel_paths": []})
        analyze = _load_json(folder / "analyze.json", {"_missing": True})
        plan = _load_json(folder / "plan.json", {"_missing": True, "page_targets": []})
        summary = score_summary(case, extract)
        results.append(
            CaseStageScore(
                case_id=case.id,
                source=case.source,
                summary=asdict(summary),
                search=_score_search(search, wiki_dir),
                analyze=_score_analyze(analyze, search),
                plan=_score_plan(plan, wiki_dir),
            )
        )
    return results


def summarize(results: list[CaseStageScore]) -> dict[str, Any]:
    def stage(name: str) -> dict[str, Any]:
        values = [getattr(r, name) for r in results]
        return {
            "passed": sum(v.passed for v in values),
            "total": len(values),
            "pass_rate": sum(v.passed for v in values) / len(values) if values else 0.0,
        }

    summary_passed = sum(r.summary["passed"] for r in results)
    return {
        "cases": len(results),
        "summary": {
            "passed": summary_passed,
            "total": len(results),
            "pass_rate": summary_passed / len(results) if results else 0.0,
        },
        "search": stage("search"),
        "analyze": stage("analyze"),
        "plan": stage("plan"),
    }
