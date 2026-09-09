#!/usr/bin/env python3
"""Evaluate the current Wiki snapshot against a private outcome dataset."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from evals.core.judge_runner import judge_one
from evals.core.outcomes import reliability_metrics
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm
from wiki_agent.log import begin_trace, finish_trace, setup_trace


async def run(args) -> dict:
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    source_root = Path(dataset["source_root"])
    wiki_dir = args.wiki_dir or Path(dataset["wiki_root"])
    client = create_llm(load_config(project_root=args.project_root).llm)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def score(case: dict, attempt: int) -> dict:
        async with semaphore:
            result = await judge_one(
                client, case, source_root=source_root, wiki_dir=wiki_dir
            )
        return {
            "case_id": case["id"],
            "attempt": attempt,
            "verdict": result["verdict"],
            "judge_verdict": result["judge_verdict"],
            "a_passed": result["a_passed"],
            "score": result["score"],
        }

    results = []
    tasks = [
        score(case, attempt)
        for case in dataset["cases"]
        for attempt in range(1, args.repeats + 1)
    ]
    for future in asyncio.as_completed(tasks):
        result = await future
        results.append(result)
        print(
            f"[{len(results)}/{len(tasks)}] {result['case_id']}"
            f"#{result['attempt']}: {result['verdict']}"
        )
    results.sort(key=lambda item: (item["case_id"], item["attempt"]))
    return {
        "component": "wiki_snapshot",
        "dataset": dataset["dataset_id"],
        "repeats": args.repeats,
        "reliability": reliability_metrics(results),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="评估当前 Wiki 快照的忠实度、覆盖率和完整性")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须大于等于 1")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    trace_id = begin_trace(f"eval_snapshot_{timestamp}")
    trace_dir = args.output.parent / "traces" / trace_id
    setup_trace(
        trace_dir,
        trace_id=trace_id,
        kind="evaluation",
        metadata={
            "dataset": args.dataset,
            "wiki_dir": args.wiki_dir,
            "repeats": args.repeats,
            "concurrency": args.concurrency,
        },
    )
    try:
        payload = asyncio.run(run(args))
    except Exception as exc:
        finish_trace("failed", error=f"{type(exc).__name__}: {exc}")
        raise
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload["trace_dir"] = str(trace_dir)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    finish_trace(
        "succeeded",
        outputs={"result": args.output},
        metrics=payload["reliability"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
