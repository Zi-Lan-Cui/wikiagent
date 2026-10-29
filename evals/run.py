"""评测统一入口。

子命令：
  corpus       校验评测语料、评分器定义与题集数据（离线，不调 LLM）
  rubric-check 评分器定义自检（离线）
  source-eval  按 source 生成评测记录（需 LLM）
  ci           全 source 评测，确定性拦截或判 fail 即退出码 1（需 LLM）
  recompile    源文件逐篇编译进新 wiki 并落 run-evidence（需 LLM）
"""

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
