#!/usr/bin/env python3
"""评估一批已记录的 QA 回答；可选接入独立 LLM Judge。

records.json 格式：[{"case_id": "qa-process-states", "answer": "...",
"tool_calls": [{"name": "ReadFile", "arguments": {}, "result": "..."}],
"citations": ["concepts/process-state.md"]}]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from evals.qa_harness import load_cases, score_answer, summarize
from evals.qa_semantic_judge import judge_qa_case
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm


def _pages(wiki_dir: Path) -> tuple[set[str], dict[str, str]]:
    pages: dict[str, str] = {}
    for path in wiki_dir.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(wiki_dir).as_posix()
        pages[rel] = path.read_text(encoding="utf-8")[:12000]
    return set(pages), pages


async def main_async(args) -> int:
    manifest = {case.id: case for case in load_cases(args.manifest)}
    records = json.loads(args.records.read_text(encoding="utf-8"))
    if isinstance(records, dict) and isinstance(records.get("records"), list):
        records = records["records"]
    if not isinstance(records, list):
        raise ValueError("records 必须是 JSON 数组")
    page_names, pages = _pages(args.wiki_dir) if args.wiki_dir else (None, {})
    hard = []
    for record in records:
        case = manifest.get(record.get("case_id"))
        if case is None:
            hard.append({"case_id": record.get("case_id"), "error": "unknown_case"})
            continue
        hard.append(
            {
                "case_id": case.id,
                "score": score_answer(case, record, existing_pages=page_names).__dict__,
            }
        )
    output = {
        "hard": hard,
        "summary": summarize(
            [
                score_answer(manifest[r["case_id"]], r, existing_pages=page_names)
                for r in records
                if r.get("case_id") in manifest
            ]
        ),
    }
    if args.semantic:
        cfg = load_config(project_root=PROJECT_ROOT)
        client = create_llm(cfg.llm, cfg.retry)
        semantic = []
        for record in records:
            case = manifest.get(record.get("case_id"))
            if case is None:
                continue
            evidence = {name: pages[name] for name in record.get("citations", []) if name in pages}
            if case_item := next(
                (
                    x
                    for x in json.loads(args.manifest.read_text(encoding="utf-8"))["cases"]
                    if x["id"] == case.id
                ),
                None,
            ):
                if case_item.get("policy_evidence"):
                    evidence["[agent-policy]"] = case_item["policy_evidence"]
            semantic.append(
                {
                    "case_id": case.id,
                    "score": await judge_qa_case(client, case_item, record, evidence),
                }
            )
        output["semantic"] = semantic
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return (
        0
        if all(item.get("score", {}).get("passed", False) for item in hard if "score" in item)
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("records", type=Path)
    parser.add_argument(
        "--manifest", type=Path, default=PROJECT_ROOT / "evals/golden/qa_manifest.json"
    )
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--output", type=Path)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
