#!/usr/bin/env python3
"""构建对抗样本并做 A 层确定性自检：注入缺陷 → 评分器是否抓得住（不调 LLM）。

这是"评测有没有牙齿"的直接证据：drop_page / strip_provenance / refuse_violation 必须被
确定性门禁抓住；fabricate_claim 故意抓不住（标为需 judge），从而下一轮 judge↔gold 一致率
真正测评分器的辨别力（false-pass）。输出 evals/adversarial.json 供 agreement/snapshot 消费。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.core.adversarial import build_adversarial_dataset, score_adversarial_deterministic


def main() -> int:
    parser = argparse.ArgumentParser(description="构建对抗样本 + A 层自检")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--per-kind", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("workspace/evals/adversarial.json"))
    args = parser.parse_args()

    base = json.loads(args.dataset.read_text(encoding="utf-8"))
    wiki = Path(base["wiki_root"])
    cases = build_adversarial_dataset(base, wiki, per_kind=args.per_kind)
    if not cases:
        print("基集不足以生成对抗例（正/负例太少或页太少）")
        return 1

    report = []
    by_kind: dict[str, list[dict]] = {}
    for case in cases:
        verdict = score_adversarial_deterministic(case)
        record = {**case, "deterministic_caught": verdict["caught"], "layer": verdict["layer"]}
        report.append(record)
        by_kind.setdefault(case["defect"], []).append(record)

    print(f"对抗例总数: {len(cases)}")
    deterministic = [c for c in report if c["defect"] != "fabricate_claim"]
    caught = [c for c in deterministic if c["deterministic_caught"]]
    print(f"A 层确定性抓住（应抓住的结构性缺陷）: {len(caught)}/{len(deterministic)}")
    fab = by_kind.get("fabricate_claim", [])
    fcaught = sum(1 for c in fab if c["deterministic_caught"])
    print(f"fabricate_claim A 层抓住（期望 0，故意留给 LLM judge）: {fcaught}/{len(fab)}")
    for kind, recs in sorted(by_kind.items()):
        cc = sum(1 for r in recs if r["deterministic_caught"])
        print(f"  - {kind:16} {cc}/{len(recs)} 确定性抓住")
    if len(caught) != len(deterministic):
        missed = [c["id"] for c in deterministic if not c["deterministic_caught"]]
        print("⚠ 结构性缺陷未被抓住（评分器有漏洞）:", missed)
    if fcaught:
        print("⚠ 编造例本应逃过 A 层却被抓住（评分器可能过紧/误报）")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"kind": "adversarial", "cases": report}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写出 {len(cases)} 条对抗例 → {args.output}")
    ok = len(caught) == len(deterministic) and fcaught == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
