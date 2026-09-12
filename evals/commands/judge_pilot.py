#!/usr/bin/env python3
"""二元判定一致性试点（B 层校准第一站）。

读取 judgements-v2 判词题集，按维度跑 binary_judge，逐条 1:1 与金标比对：
- 分维度一致率（排除 unknown）——四维度各自 ≥85% 才达标；
- unknown 率——弃权不计入通过率；
- false-positive / false-negative 与判定漂移率。

用法:
    uv run python -m evals.commands.judge_pilot [gold.json] \
        [--out workspace/evals/judge-pilot-<date>.json] [--repeats 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.judges.binary_judge import judge_claims
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm

_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "evals" / "corpora" / "reference-v1"


def _default_gold() -> Path:
    return _CORPUS_ROOT / "verdicts" / "judgements-v2.json"


def _load_files(root: Path, paths: list[str]) -> str:
    """无脑文件加载器——按 view 里的文件清单逐个读，拼成判定输入。

    view.inputs / view.output 是编译那一刻 compiler 真实可见/产出的
    文件清单（相对 corpus 根）。加载器不做任何 stage 特判、不解析内容
    结构——文件里是什么就喂什么，信息量对齐由题目文件保证。
    """
    parts: list[str] = []
    for relative in paths:
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise FileNotFoundError(f"view 文件不存在: {relative}")
        # JSON 文件保持结构化原样（plan/analyze/index 快照本来就是
        # 结构化记录），文本文件直接读
        content = path.read_text(encoding="utf-8")
        parts.append(f"### {relative}\n{content[:12000]}")
    return "\n\n".join(parts)


async def _run(client, root: Path, cases: list[dict], repeats: int = 1) -> dict:
    inputs: dict[tuple, str] = {}
    outputs: dict[tuple, str] = {}
    results: list[dict] = []
    for case in cases:
        view = case["view"]
        input_key = tuple(view["inputs"])
        inputs.setdefault(input_key, _load_files(root, view["inputs"]))
        output_key = tuple(view["output"])
        outputs.setdefault(output_key, _load_files(root, view["output"]))
    for case in cases:
        verdicts: list = []
        raws: list = []
        for _ in range(repeats):
            try:
                judged = await judge_claims(
                    client,
                    inputs[tuple(case["view"]["inputs"])],
                    outputs[tuple(case["view"]["output"])],
                    [case],
                )
                verdicts.append(judged["verdicts"].get(case["id"]))
                raws.append(judged.get("raw"))
            except Exception as exc:  # LLM 调用失败——记 unknown，不让整批挂
                verdicts.append(None)
                raws.append({"error": str(exc)})
        # 多遍判定: 多数票为终判；平票或全 unknown → unknown
        decided_votes = [v for v in verdicts if v is not None]
        if not decided_votes:
            final = None
        else:
            true_votes = sum(bool(v) for v in decided_votes)
            false_votes = len(decided_votes) - true_votes
            final = None if true_votes == false_votes else true_votes > false_votes
        results.append({**case, "judge": final, "verdicts": verdicts, "raw": raws[-1]})
    return _summarize(results)


def _summarize(results: list[dict]) -> dict:
    per_dimension: dict[str, dict] = {}
    for item in results:
        dim = item["dimension"]
        bucket = per_dimension.setdefault(
            dim, {"total": 0, "decided": 0, "unknown": 0, "agreed": 0,
                  "false_positive": 0, "false_negative": 0}
        )
        bucket["total"] += 1
        gold = item["gold"]
        judge = item["judge"]
        if judge is None:
            bucket["unknown"] += 1
        elif bool(judge) == bool(gold):
            bucket["agreed"] += 1
            bucket["decided"] += 1
        elif bool(judge) and not bool(gold):
            bucket["false_positive"] += 1
            bucket["decided"] += 1
        else:
            bucket["false_negative"] += 1
            bucket["decided"] += 1
    for dim, bucket in per_dimension.items():
        bucket["agreement_rate"] = (
            round(bucket["agreed"] / bucket["decided"], 4) if bucket["decided"] else None
        )
    total = sum(b["agreed"] for b in per_dimension.values())
    decided = sum(b["decided"] for b in per_dimension.values())
    return {
        "total": len(results),
        "decided": decided,
        "agreed": total,
        "agreement_rate": round(total / decided, 4) if decided else None,
        "dimensions": per_dimension,
        "results": results,
    }


def _stability(results: list[dict]) -> dict:
    """判定漂移率——同一 claim 多遍判定不一致的比例（judge 稳定性指标）。"""
    drifted = []
    for item in results:
        votes = item.get("verdicts") or []
        decided = [v for v in votes if v is not None]
        if len(set(decided)) > 1:
            drifted.append(item["id"])
    return {
        "drifted": len(drifted),
        "drifted_ids": drifted,
        "drift_rate": round(len(drifted) / len(results), 4) if results else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="二元判定一致性试点")
    parser.add_argument("gold", type=Path, nargs="?", default=_default_gold())
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--repeats", type=int, default=1, help="每条 claim 判定重复次数（测稳定性）")
    args = parser.parse_args()

    gold_path = args.gold.resolve()
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    cases = gold["cases"]
    # corpus root = verdicts/ 的上一级（gold 文件约定放在 corpora/reference-v1/verdicts/）
    root = gold_path.parent.parent if gold_path.parent.name == "verdicts" else _CORPUS_ROOT
    client = create_llm(load_config(project_root=Path.cwd()).llm)
    summary = asyncio.run(_run(client, root, cases, repeats=args.repeats))

    payload = {
        "component": "binary_judge",
        "set_id": gold["set_id"],
        "repeats": args.repeats,
        "summary": {
            **{k: v for k, v in summary.items() if k != "results"},
            **_stability(summary["results"]),
        },
        "results": summary["results"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(f"已写入: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
