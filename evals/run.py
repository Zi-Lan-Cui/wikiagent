"""评测统一入口。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 支持 `python evals/run.py` 直跑

# 值为 (命令模块, 附加参数)：ci 复用 source_eval，靠注入 --ci 区分行为，
# 而非再写一个只改退出码的孪生模块
_DISPATCH = {
    "corpus": ("reference_corpus", []),
    "rubric-check": ("rubric_check", []),
    "source-eval": ("source_eval", []),
    "ci": ("source_eval", ["--ci"]),
    "recompile": ("recompile", []),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="评测统一入口")
    parser.add_argument("command", choices=_DISPATCH)
    args, extra = parser.parse_known_args()
    module, injected = _DISPATCH[args.command]
    return subprocess.run(
        [sys.executable, "-m", f"evals.commands.{module}", *injected, *extra], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
