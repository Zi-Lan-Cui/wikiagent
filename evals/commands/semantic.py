#!/usr/bin/env python3
"""使用配置中的 LLM 对编译产物进行语义评估。

支持两种 manifest:

* ``evals/templates/manifest.json``：用户填写的 cluster/事实约束清单；
* ``evals/templates/source_manifest.json``：用户填写的 source 批量清单，
  组织参考从每条 source 的 plan 动态读取。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, cast

from evals.core.harness import notebook_root
from evals.core.paths import PROJECT_ROOT, require_directory, require_file
from evals.judges.semantic_judge import judge_case
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm


def _artifact(folder: Path, name: str, default: object) -> object:
    try:
        return json.loads((folder / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _pages(folder: Path) -> list[dict[str, str]]:
    result = []
    for path in sorted((folder / "pages").glob("*.md")):
        result.append({"path": path.name, "content": path.read_text(encoding="utf-8")[:7000]})
    return result


def _artifact_folder(run_dirs: Path | list[Path], case: dict[str, object]) -> Path:
    """定位 artifact；兼容旧 basename 布局和 120 条 source-id 布局。"""
    dirs = [run_dirs] if isinstance(run_dirs, Path) else run_dirs
    source = str(case["source"])
    case_id = str(case["id"])
    fallback = dirs[0] / "artifacts" / Path(source).name
    for run_dir in dirs:
        artifacts = run_dir / "artifacts"
        direct = artifacts / Path(source).name
        if direct.is_dir():
            return direct
        prefix_matches = sorted(artifacts.glob(f"{case_id}__*"))
        if prefix_matches:
            return prefix_matches[0]
    return fallback


def _load_cases(manifest_path: Path) -> tuple[list[dict], bool]:
    """加载用户提供的标准 manifest，并返回是否为批量 source 集。"""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(data.get("cases"), list):
        return data["cases"], False
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"manifest 必须包含 cases 或非空 sources: {manifest_path}")
    cases = []
    for source in sources:
        if not isinstance(source, dict) or not source.get("id") or not source.get("path"):
            raise ValueError("source manifest 每项必须包含 id 和 path")
        cases.append(
            {
                "id": source["id"],
                "source": source["path"],
                "must_include": [],
                "must_not_invent": [],
                "cluster": {
                    "goal": f"评估 {source.get('category', '')} 中的《{source.get('title', source['path'])}》",
                    "expected_pages": [],
                    "expected_relations": [],
                },
            }
        )
    return cases, True


def _wiki_dir_for_run(run_dir: Path, explicit: Path | None) -> Path:
    if explicit:
        return explicit
    # <wiki>/.logs/runs/<run_id>
    return run_dir.resolve().parents[2]


def _load_run_dirs(args) -> list[Path]:
    """从单个 run 或 compile_manifest state 展开所有批次 run。"""
    if not args.state:
        return [args.run_dir]
    payload = json.loads(args.state.read_text(encoding="utf-8"))
    dirs = [Path(item["run_dir"]) for item in payload.get("batches", []) if item.get("run_dir")]
    if not dirs:
        raise ValueError(f"state 没有可用的 batches.run_dir: {args.state}")
    return dirs


def _pages_from_plan(folder: Path, wiki_dir: Path, plan: dict) -> list[dict[str, str]]:
    """从 plan 的页面目标读取最终 Wiki 页面，而非假定 artifact/pages 存在。"""
    result = []
    targets = plan.get("page_targets", []) if isinstance(plan, dict) else []
    for target in targets:
        if not isinstance(target, dict):
            continue
        path = str(target.get("wiki_path", ""))
        page = wiki_dir / path.removeprefix("wiki/")
        if page.is_file():
            result.append({"path": path, "content": page.read_text(encoding="utf-8")[:7000]})
    if result:
        return result
    return _pages(folder)


def _write_progress(path: Path | None, payload: dict) -> None:
    """立即保存当前状态，避免长任务只有结束时才产生结果文件。"""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(args) -> dict:
    manifest_path = require_file(args.manifest, kind="semantic manifest")
    cases, source_manifest = _load_cases(manifest_path)
    clusters = {
        c["id"]: c
        for c in json.loads(manifest_path.read_text(encoding="utf-8")).get("clusters", [])
    }
    root = notebook_root(Path(args.notebook_root) if args.notebook_root else None)
    cfg = load_config(project_root=PROJECT_ROOT)
    client = create_llm(cfg.llm, cfg.retry)
    semaphore = asyncio.Semaphore(args.concurrency)
    run_dirs = _load_run_dirs(args)
    payload = {
        "run_dir": str(args.run_dir),
        "state": str(args.state) if args.state else None,
        "manifest": str(manifest_path),
        "completed": 0,
        "total": len(cases),
        "cases": [{"case_id": case["id"], "status": "pending"} for case in cases],
    }
    _write_progress(args.output, payload)

    async def score_one(case: dict) -> dict:
        folder = _artifact_folder(run_dirs, case)
        try:
            source = (root / case["source"]).read_text(encoding="utf-8")
            plan = cast(dict[str, Any], _artifact(folder, "plan.json", {}))
            artifacts = {
                "extract": (folder / "extract.json").read_text(encoding="utf-8")[:24000]
                if (folder / "extract.json").exists()
                else "",
                "search": _artifact(folder, "search.json", {}),
                "analyze": _artifact(folder, "analyze.json", {}),
                "plan": plan,
            }
            if source_manifest:
                case_input = {**case}
                case_input["cluster"] = {
                    **case["cluster"],
                    "expected_pages": [
                        t.get("wiki_path")
                        for t in plan.get("page_targets", [])
                        if isinstance(t, dict) and t.get("wiki_path")
                    ],
                }
            else:
                case_input = {**case, "cluster": clusters[case["cluster"]]}
            async with semaphore:
                score = await judge_case(
                    client,
                    case_input,
                    source,
                    artifacts,
                    _pages_from_plan(folder, _wiki_dir_for_run(args.run_dir, args.wiki_dir), plan),
                )
            return {"case_id": case["id"], "status": "scored", "score": score}
        except Exception as exc:
            return {"case_id": case["id"], "status": "judge_error", "error": str(exc)}

    tasks = [asyncio.create_task(score_one(case)) for case in cases]
    results_by_id: dict[str, dict] = {}
    for future in asyncio.as_completed(tasks):
        result = await future
        results_by_id[result["case_id"]] = result
        payload["completed"] = len(results_by_id)
        payload["cases"] = [
            results_by_id.get(case["id"], {"case_id": case["id"], "status": "pending"})
            for case in cases
        ]
        _write_progress(args.output, payload)
        print(
            f"[{payload['completed']}/{payload['total']}] {result['case_id']}: {result['status']}",
            flush=True,
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--state", type=Path, help="compile_manifest 的 state.json；跨批次评估全部 source"
    )
    parser.add_argument(
        "--manifest", type=Path, default=PROJECT_ROOT / "evals/templates/manifest.json"
    )
    parser.add_argument("--notebook-root", type=Path)
    parser.add_argument("--wiki-dir", type=Path, help="Wiki 根目录；默认从 run_dir 推导")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency 必须大于 0")
    args.run_dir = require_directory(args.run_dir, kind="compile run 目录")
    if args.state:
        args.state = require_file(args.state, kind="compile state")
    if args.notebook_root:
        args.notebook_root = require_directory(args.notebook_root, kind="笔记根目录")
    if args.wiki_dir:
        args.wiki_dir = require_directory(args.wiki_dir, kind="Wiki 根目录")
    if args.output:
        args.output = args.output.expanduser().resolve()
    print(
        f"开始 semantic eval: manifest={args.manifest} "
        f"concurrency={args.concurrency} output={args.output or '<stdout>'}",
        flush=True,
    )
    result = asyncio.run(run(args))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if all(item["status"] == "scored" for item in result["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
