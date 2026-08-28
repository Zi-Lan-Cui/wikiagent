#!/usr/bin/env python3
"""把 source manifest 的真实笔记复制到扁平临时目录供 compile 使用。"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=Path("evals/golden/source_manifest_120.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    for item in data["sources"]:
        source = args.root / item["path"]
        target = args.output / f"{item['id']}__{source.name}"
        shutil.copy2(source, target)
    print(f"materialized {len(data['sources'])} sources: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
