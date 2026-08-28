#!/usr/bin/env python3
"""对一次 refine run 运行 Refine 专用 LLM Judge。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from evals.refine_semantic_judge import judge_refine_case
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm


def _json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _payload(folder: Path, wiki_dir: Path) -> dict:
    meta = _json(folder / "meta.json", {})
    source = meta.get("source", folder.name)
    before = (
        (folder / "page_before.md").read_text(encoding="utf-8")
        if (folder / "page_before.md").exists()
        else ""
    )
    after = (
        (folder / "page_after.md").read_text(encoding="utf-8")
        if (folder / "page_after.md").exists()
        else ""
    )
    search = _json(folder / "search.json", {})
    candidates = []
    for rel in search.get("rel_paths", []) if isinstance(search, dict) else []:
        path = wiki_dir / str(rel).removeprefix("wiki/")
        if path.is_file():
            candidates.append(
                {"path": str(rel), "content": path.read_text(encoding="utf-8")[:7000]}
            )
    return {
        "source": source,
        "status": meta.get("status", "unknown"),
        "before": before[:18000],
        "after": after[:18000],
        "search": search,
        "analyze": _json(folder / "analyze.json", {}),
        "plan": _json(folder / "plan.json", {}),
        "candidate_pages": candidates,
    }


async def run(args) -> dict:
    cfg = load_config(project_root=PROJECT_ROOT)
    client = create_llm(cfg.llm, cfg.retry)
    semaphore = asyncio.Semaphore(3)
    folders = [p for p in sorted((args.run_dir / "artifacts").iterdir()) if p.is_dir()]

    async def one(folder: Path) -> dict:
        payload = _payload(folder, args.wiki_dir)
        try:
            async with semaphore:
                score = await judge_refine_case(client, payload)
            return {"source": payload["source"], "status": "scored", "score": score}
        except Exception as exc:
            return {"source": payload["source"], "status": "judge_error", "error": str(exc)}

    return {"run_dir": str(args.run_dir), "cases": await asyncio.gather(*(one(p) for p in folders))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if all(x["status"] == "scored" for x in result["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
