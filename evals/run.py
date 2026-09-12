"""评测统一入口。

子命令：
  corpus      校验评测语料与判词题集（离线，不调 LLM）
  judge-pilot 运行 Judge 校准：judge 判词与专家金标 1:1 比对（需 LLM）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 支持 `python evals/run.py` 直跑

_DISPATCH = {
    "corpus": "reference_corpus",
    "judge-pilot": "judge_pilot",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="评测统一入口")
    parser.add_argument("command", choices=_DISPATCH)
    args, extra = parser.parse_known_args()
    return subprocess.run(
        [sys.executable, "-m", f"evals.commands.{_DISPATCH[args.command]}", *extra], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
