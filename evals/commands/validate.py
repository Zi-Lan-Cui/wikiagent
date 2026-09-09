#!/usr/bin/env python3
"""校验一个已存在的 v3 数据集：schema 合法 + 每条参考解过确定性门禁。不调 LLM。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.core.dataset import assert_all_references_pass, validate_schema_v3


def main() -> int:
    parser = argparse.ArgumentParser(description="校验评测数据集")
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    errors = validate_schema_v3(data)
    failures = [] if errors else assert_all_references_pass(data, Path(data["wiki_root"]))
    if errors:
        print("✗ schema 非法:")
        for e in errors[:20]:
            print("   -", e)
        return 1
    if failures:
        print(f"✗ {len(failures)} 例参考解未过确定性门禁:")
        for f in failures[:20]:
            print("   -", f)
        return 1
    print(f"✅ {data.get('case_count', len(data['cases']))} 例：schema 合法 + 参考解全部过确定性门禁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
