#!/usr/bin/env python3
"""评估一次真实 compile run 的 Extract/Search/Analyze/Plan 阶段。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from evals.core.stage_harness import score_run, summarize


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = score_run(args.run_dir, args.wiki_dir)
    payload = {"summary": summarize(results), "cases": [asdict(r) for r in results]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return (
        0
        if all(getattr(r, stage).passed for r in results for stage in ("search", "analyze", "plan"))
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
