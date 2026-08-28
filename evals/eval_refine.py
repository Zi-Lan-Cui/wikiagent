#!/usr/bin/env python3
"""评估一次 refine run 的确定性边界。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.refine_harness import score_refine_run, summarize_refine


def main() -> int:
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
