#!/usr/bin/env python3
"""评估一次 refine run 的确定性边界。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from evals.core.paths import PROJECT_ROOT, require_directory
from evals.core.refine_harness import score_refine_run, summarize_refine
from evals.judges.refine_semantic_judge import judge_refine_case
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm


def _json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def live_run(args: argparse.Namespace) -> dict:
    cfg = load_config(project_root=PROJECT_ROOT)
    client = create_llm(cfg.llm, cfg.retry)
    folders = [p for p in sorted((args.run_dir / "artifacts").iterdir()) if p.is_dir()]

    async def one(folder: Path) -> dict:
        meta = cast(dict[str, Any], _json(folder / "meta.json", {}))
        payload = {
            "source": meta.get("source", folder.name),
            "status": meta.get("status", "unknown"),
            "before": _text(folder / "page_before.md"),
            "after": _text(folder / "page_after.md"),
            "search": _json(folder / "search.json", {}),
            "analyze": _json(folder / "analyze.json", {}),
            "plan": _json(folder / "plan.json", {}),
            "candidate_pages": [],
        }
        try:
            return {
                "source": payload["source"],
                "status": "scored",
                "score": await judge_refine_case(client, payload),
            }
        except Exception as exc:
            return {"source": payload["source"], "status": "judge_error", "error": str(exc)}

    return {"run_dir": str(args.run_dir), "cases": await asyncio.gather(*(one(p) for p in folders))}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        parser = argparse.ArgumentParser(description="运行 Refine LLM 语义评测")
        parser.add_argument("run_dir", type=Path)
        parser.add_argument("--wiki-dir", type=Path, required=True)
        parser.add_argument("--output", type=Path)
        args = parser.parse_args(sys.argv[2:])
        args.run_dir = require_directory(args.run_dir, kind="refine run 目录")
        args.wiki_dir = require_directory(args.wiki_dir, kind="Wiki 根目录")
        result = asyncio.run(live_run(args))
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        print(text)
        return 0 if all(x["status"] == "scored" for x in result["cases"]) else 1
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path, required=True)
    args = parser.parse_args()
    results = score_refine_run(args.run_dir, args.wiki_dir)
    payload = {"summary": summarize_refine(results), "cases": [asdict(r) for r in results]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
