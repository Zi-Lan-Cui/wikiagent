"""评测公共入口：一次命令调用常用离线评测。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from evals.core.results import aggregate_reports


def main() -> int:
    parser = argparse.ArgumentParser(description="评测统一入口")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("all", "summary", "verify", "qa", "refine", "stages", "semantic", "report"),
        default="all",
    )
    parser.add_argument("--quick", action="store_true", help="all 模式只运行 harness")
    args, extra = parser.parse_known_args()

    if args.command == "report":
        inputs = [Path(item) for item in extra if not item.startswith("--")]
        if not inputs:
            parser.error("report 至少需要一个组件结果 JSON")
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in inputs]
        print(json.dumps(aggregate_reports(reports), ensure_ascii=False, indent=2))
        return 0

    if args.command not in {"all", "summary"}:
        module = {
            "verify": "check",
            "qa": "qa",
            "refine": "refine",
            "stages": "stages",
            "semantic": "semantic",
        }[args.command]
        return subprocess.run(
            [sys.executable, "-m", f"evals.commands.{module}", *extra], check=False
        ).returncode

    command = [sys.executable, "-m", "evals.commands.check", "--summary"]
    result = subprocess.run(command, check=False)
    if result.returncode:
        return result.returncode
    if args.quick:
        return 0
    return subprocess.run(["uv", "run", "pytest", "-q"], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
