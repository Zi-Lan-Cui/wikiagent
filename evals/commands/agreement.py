#!/usr/bin/env python3
"""LLM judge ↔ 人工金标一致率：跑 judge 一次/例，比对 human_verdict，出 agreement 报告。

一致率的"真值"必须是人工裁决（label.py 签核回写），模型自比无意义。本轮只对
有 human_verdict 的正例(capability)跑 judge；负例是确定性证据，不在此判定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.core import reporting
from evals.core.judge_runner import judge_one
from evals.core.results import aggregate_reports
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm


def _judgeable(case: dict) -> bool:
    # 只把【有金标且语义 judge 能判】的 case 纳入一致率：正例（对真实 wiki）
    # 与对抗例（带内联 candidate_pages）。纯净负例走确定性 no-op 门禁，不参与 judge。
    if not case.get("human_verdict"):
        return False
    return case.get("polarity") == "positive" or "candidate_pages" in case


async def _run(cases: list[dict], client, wiki_dir: Path, source_root: Path) -> list[dict]:
    semaphore = asyncio.Semaphore(2)
    targets = [case for case in cases if _judgeable(case)]

    async def one(case: dict) -> dict:
        async with semaphore:
            result = await judge_one(
                client, case, source_root=source_root, wiki_dir=wiki_dir
            )
        return {
            "id": case["id"],
            "verdict": result["verdict"],  # judge 预测
            "expected_verdict": case["human_verdict"],  # 人工真值
            "judge_verdict": result["judge_verdict"],
        }

    return await asyncio.gather(*(one(case) for case in targets))


def main() -> int:
    parser = argparse.ArgumentParser(description="计算 LLM judge 与人工金标的一致率")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--adversarial", type=Path, default=Path("workspace/evals/adversarial.json"))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    pool = list(dataset["cases"])
    if args.adversarial.is_file():  # 并入对抗例（gold=fail），使一致率真正测 judge false-pass
        pool += json.loads(args.adversarial.read_text(encoding="utf-8")).get("cases", [])
    if not any(_judgeable(c) for c in pool):
        print(
            "⚠ 没有可判的已签核 case：先跑 label 交互标注 + `make eval-apply-labels`，"
            "并 `evals/commands/adversarial.py` 生成对抗例。"
        )
        return 2

    client = create_llm(load_config(project_root=args.project_root).llm)
    wiki_dir = Path(dataset["wiki_root"])
    source_root = Path(dataset["source_root"])
    cases = asyncio.run(_run(pool, client, wiki_dir, source_root))

    report = aggregate_reports([{"component": "semantic_judge", "cases": cases}])
    payload = {
        "component": "semantic_judge",
        "cases": cases,
        "summary": report["components"]["semantic_judge"],
        "status": report["status"],
    }
    date_str = args.date or reporting.today_slug()
    out_dir = args.out_dir or (reporting.REPORTS_DIR / date_str)
    reporting.write_json(out_dir / "agreement.json", payload)
    agree = reporting.summarize_agreement(payload)
    print(
        f"judge↔人工：已判 {agree['labeled_cases']} 例，一致率 {agree['agreement']:.1%}"
        f"（{'≥85% 达标' if agree['meets_85_gate'] else '<85% 需继续校准'}）"
        f" → {out_dir / 'agreement.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
