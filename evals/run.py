"""评测统一入口（v3）：离线自检 + 分层判定 + 一致率 + 报告。

只评结果（outcome）；判定 = A(确定性门禁) ∧ B(LLM 语义 judge)。子命令：
  dataset     从 wiki/provenance 生成 v3 数据集并自检（离线）
  validate    校验已有数据集 schema + 参考解过确定性门禁（离线）
  adversarial 注入已知缺陷 + A 层自检（离线，证明评测有牙齿）
  snapshot    对当前 wiki 跑分层判定 + pass^k（需 LLM）
  label       人工金标工作表/回写
  agreement   judge↔人（+对抗）一致率、混淆矩阵（需 LLM）
  headline    汇总 HEADLINE 报告（离线）
  report      聚合任意组件结果 JSON（离线）
  all         dataset → adversarial（离线，不花 API）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 支持 `python evals/run.py` 直跑

from evals.core.results import aggregate_reports

_DISPATCH = {
    "dataset": "build_dataset",
    "validate": "validate",
    "adversarial": "adversarial",
    "snapshot": "snapshot",
    "label": "label",
    "agreement": "agreement",
    "headline": "report",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="评测统一入口")
    parser.add_argument("command", nargs="?", default="all", choices=(*_DISPATCH, "all", "report"))
    args, extra = parser.parse_known_args()

    if args.command == "report":
        return _report(extra, parser)
    if args.command == "all":
        for cmd in ("dataset", "adversarial"):
            rc = subprocess.run(
                [sys.executable, "-m", f"evals.commands.{_DISPATCH[cmd]}", *extra], check=False
            ).returncode
            if rc:
                return rc
        return 0
    return subprocess.run(
        [sys.executable, "-m", f"evals.commands.{_DISPATCH[args.command]}", *extra], check=False
    ).returncode


def _report(extra: list[str], parser: argparse.ArgumentParser) -> int:
    write, inputs, pending = None, [], False
    for item in extra:
        if pending:
            write, pending = Path(item), False
        elif item == "--write":
            pending = True
        elif not item.startswith("--"):
            inputs.append(Path(item))
    if not inputs:
        parser.error("report 至少需要一个组件结果 JSON")
    aggregate = aggregate_reports(
        [json.loads(path.read_text(encoding="utf-8")) for path in inputs]
    )
    if write is not None:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
