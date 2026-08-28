#!/usr/bin/env python3
"""黄金集完整性和摘要硬约束评估入口。

用法:
    WIKI_NOTEBOOK_ROOT=/path/to/notebook uv run python evals/run.py --verify
    uv run python evals/run.py --summary

真实 LLM 调用暂不由此入口自动触发，避免误产生费用或修改 Wiki。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.core.harness import load_cases, manifest_summary, verify_sources
from evals.core.paths import require_file


def main() -> int:
    parser = argparse.ArgumentParser(description="wiki-agent golden eval harness")
    parser.add_argument("--verify", action="store_true", help="验证外部笔记文件和 hash")
    parser.add_argument("--summary", action="store_true", help="输出黄金集概况")
    parser.add_argument("--manifest", type=Path, help="可选的黄金集 JSON；默认使用仓库内固定清单")
    args = parser.parse_args()
    if not args.verify and not args.summary:
        parser.error("至少指定 --verify 或 --summary")

    manifest = require_file(args.manifest, kind="manifest") if args.manifest else None
    cases = load_cases(manifest) if manifest else load_cases()
    if not cases:
        parser.error("manifest 不包含 cases；请先用模板构建至少一个评测样本")
    if args.summary:
        print(
            json.dumps(
                manifest_summary(manifest) if manifest else manifest_summary(),
                ensure_ascii=False,
                indent=2,
            )
        )
        for case in cases:
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
