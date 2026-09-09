#!/usr/bin/env python3
"""汇总评测证据为 evals/reports/<date>/HEADLINE.md（+ manifest.json）。

读数据集（确定性自检证据）+ 可选的 agreement.json / snapshot.json，渲染一张可提交的
滚动结论表。纯离线、不调 LLM。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evals.core import reporting
from evals.core.dataset import assert_all_references_pass, validate_schema_v3


def _load(path: Path | None) -> dict[str, Any] | None:
    if path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def build_parts(dataset: dict[str, Any], wiki_root: Path) -> dict[str, Any]:
    parts: dict[str, Any] = {"dataset": dataset}
    errors = validate_schema_v3(dataset)
    ref_failures = [] if errors else assert_all_references_pass(dataset, wiki_root)
    parts["references_pass"] = not errors and not ref_failures
    parts["schema_errors"] = errors
    parts["reference_failures"] = ref_failures
    parts["negatives"] = reporting.negative_evidence(dataset)
    return parts


def main() -> int:
    parser = argparse.ArgumentParser(description="生成评测 HEADLINE 报告")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--agreement", type=Path, default=None)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--date", type=str, default=None)
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    parts = build_parts(dataset, Path(dataset["wiki_root"]))
    agree = _load(args.agreement)
    if agree:
        parts["agreement"] = reporting.summarize_agreement(agree)
    snap = _load(args.snapshot)
    if snap:
        parts["reliability"] = snap.get("reliability")

    date_str = args.date or reporting.today_slug()
    headline = reporting.render_headline(date_str, parts)
    reporting.write_json(
        headline.parent / "manifest.json",
        {
            "date": date_str,
            "dataset_id": dataset.get("dataset_id"),
            "case_count": dataset.get("case_count"),
            "positive_count": dataset.get("positive_count"),
            "negative_count": dataset.get("negative_count"),
            "references_pass": parts["references_pass"],
            "has_agreement": bool(agree),
            "has_snapshot": bool(snap),
        },
    )
    print(f"HEADLINE 已生成: {headline}")
    if not parts["references_pass"]:
        print("⚠ 参考解自检未全过——见 HEADLINE 前，先修数据集/评分器")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
