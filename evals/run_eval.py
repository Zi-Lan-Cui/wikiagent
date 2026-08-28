#!/usr/bin/env python3
"""黄金集完整性和摘要硬约束评估入口。

用法:
    WIKI_NOTEBOOK_ROOT=/path/to/notebook uv run python evals/run_eval.py --verify
    uv run python evals/run_eval.py --summary

真实 LLM 调用暂不由此入口自动触发，避免误产生费用或修改 Wiki。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.harness import load_cases, manifest_summary, verify_sources


def main() -> int:
    parser = argparse.ArgumentParser(description="wiki-agent golden eval harness")
    parser.add_argument("--verify", action="store_true", help="验证外部笔记文件和 hash")
    parser.add_argument("--summary", action="store_true", help="输出黄金集概况")
    args = parser.parse_args()
    if not args.verify and not args.summary:
        parser.error("至少指定 --verify 或 --summary")

    if args.summary:
        print(json.dumps(manifest_summary(), ensure_ascii=False, indent=2))
        for case in load_cases():
            print(f"- {case.id}: {case.source}")
    if args.verify:
        errors = verify_sources()
        if errors:
            print("黄金样本校验失败:")
            print("\n".join(f"  - {error}" for error in errors))
            return 1
        print(f"黄金样本校验通过：{len(load_cases())} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
