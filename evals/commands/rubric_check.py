"""评分器自检——解析定义文件、打印阶段/维度/规则概览，异常退出码 1。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.core.reference_models import load_grader

_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "rubrics" / "stage-rubrics-v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="评分器定义自检")
    parser.add_argument("grader", type=Path, nargs="?", default=_DEFAULT_PATH)
    args = parser.parse_args()
    try:
        grader = load_grader(args.grader)
    except (OSError, ValueError) as exc:
        print(f"评分器定义不可用: {exc}", file=sys.stderr)
        return 1
    print(f"{grader.schema_version}: {args.grader.name}")
    for stage, rubric in sorted(grader.stage_rubrics.items(), key=lambda item: item[1].seq):
        dimensions = "、".join(f"{d.id}({d.weight:.2f})" for d in rubric.dimensions)
        rules = grader.deterministic_checks.get(stage, [])
        print(f"  {stage} seq={rubric.seq} 维度: {dimensions} | 确定性规则: {len(rules)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
