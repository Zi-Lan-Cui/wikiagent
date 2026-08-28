"""refine 的确定性评测：只更新当前页，且失败不产生半成品。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RefineCaseScore:
    source: str
    passed: bool
    checks: dict[str, bool]
    metrics: dict[str, int | float]
    failures: list[str]


def _json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def score_refine_run(run_dir: Path, wiki_dir: Path) -> list[RefineCaseScore]:
    results: list[RefineCaseScore] = []
    for folder in sorted((run_dir / "artifacts").iterdir()):
        if not folder.is_dir():
            continue
        meta = _json(folder / "meta.json")
        source = meta.get("source", folder.name)
        before = folder / "page_before.md"
        after = folder / "page_after.md"
        plan = _json(folder / "plan.json")
        targets = plan.get("page_targets", [])
        target_paths = [
            str(t.get("wiki_path", "")).removeprefix("wiki/").removesuffix(".md")
            for t in targets
            if isinstance(t, dict)
        ]
        current = source.removesuffix(".md")
        failures: list[str] = []
        checks = {
            "source_recorded": bool(source),
            "before_snapshot": before.is_file(),
            "after_snapshot_on_success": meta.get("status") != "completed" or after.is_file(),
            "self_update_only": all(path == current for path in target_paths),
            "update_only": all(
                t.get("disposition") == "update" for t in targets if isinstance(t, dict)
            ),
            "no_duplicate_targets": len(target_paths) == len(set(target_paths)),
            # refine 的 source 是知识页本身；不应在该页面 artifact 下出现
            # 新的 sources 产物。已有 sources 档案页可以继续保留。
            "no_source_artifact": not (folder / "sources").exists(),
        }
        if meta.get("status") == "failed" and after.exists():
            checks["failed_has_no_after"] = False
        else:
            checks["failed_has_no_after"] = True
        messages = {
            "source_recorded": "缺少当前页面身份",
            "before_snapshot": "缺少 refine 前快照",
            "after_snapshot_on_success": "成功页面缺少 refine 后快照",
            "self_update_only": "plan 试图更新当前页之外的页面",
            "update_only": "refine plan 含非 update 动作",
            "no_duplicate_targets": "plan 含重复目标",
            "no_source_artifact": "refine 产物中出现了 sources 页面",
            "failed_has_no_after": "失败页面存在 refine 后快照，疑似半成品",
        }
        failures.extend(messages[k] for k, ok in checks.items() if not ok)
        results.append(
            RefineCaseScore(
                source=source,
                passed=not failures,
                checks=checks,
                metrics={
                    "target_count": len(target_paths),
                    "changed": int(
                        before.is_file()
                        and after.is_file()
                        and before.read_bytes() != after.read_bytes()
                    ),
                },
                failures=failures,
            )
        )
    return results


def summarize_refine(results: list[RefineCaseScore]) -> dict[str, Any]:
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": sum(r.passed for r in results) / len(results) if results else 0.0,
        "changed": sum(r.metrics["changed"] for r in results),
        "failed_cases": [r.source for r in results if not r.passed],
    }
