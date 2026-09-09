#!/usr/bin/env python3
"""人机一致率的"人工"半边：导出标注工作表 / 回写已签核金标。

judge-vs-自己(模型起草)的一致率毫无意义——必须由开发者裁决。工作表让裁决轻量:
逐行填 human_verdict，再 --apply 一个 JSON 回写进数据集并置 human_ratified。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

VALID = {"pass", "fail", "review"}
_ABBREV = {"p": "pass", "f": "fail", "r": "review", "": "pass"}


def write_worksheet(dataset: dict[str, Any], out: Path) -> int:
    lines = [
        "# 评测金标工作表（人工裁决）",
        "",
        "> 每例填 `human_verdict: pass|fail|review`（judge 应给出什么结论才算对）与 `reviewer`。",
        "> 填好后另存为 JSON `{\"<id>\": {\"human_verdict\": \"pass\", \"reviewer\": \"you\"}}`，",
        "> 用 `label.py --apply` 回写数据集。",
        "",
    ]
    for case in dataset.get("cases", []):
        facts = "；".join(f.get("assertion", "") for f in case.get("required_facts", [])) or "（无）"
        forbidden = "；".join(case.get("forbidden_claims", [])) or "（无）"
        lines += [
            f"## {case['id']}  ·  {case.get('polarity')}/{case.get('suite')}",
            f"- source: `{case.get('source')}`",
            f"- expected_pages: {case.get('expected_pages')}",
            f"- required_facts: {facts}",
            f"- forbidden_claims: {forbidden}",
            "- human_verdict: ",
            "- reviewer: ",
            "- notes: ",
            "",
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(dataset.get("cases", []))


def apply_labels(dataset: dict[str, Any], ratify: dict[str, Any]) -> int:
    applied = 0
    for case in dataset.get("cases", []):
        entry = ratify.get(case["id"])
        if not entry:
            continue
        verdict = entry.get("human_verdict")
        if verdict not in VALID:
            raise ValueError(f"{case['id']}: human_verdict 非法 {verdict!r}")
        case["human_verdict"] = verdict
        case["reviewer"] = entry.get("reviewer", "unknown")
        annotation = case.setdefault("annotation", {})
        annotation["review_status"] = "human_ratified"
        if entry.get("notes"):
            annotation["notes"] = entry["notes"]
        applied += 1
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="导出/回写人工金标，或交互式逐条裁决")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--worksheet", type=Path, help="导出静态工作表（markdown）")
    parser.add_argument("--apply", type=Path, help="从此 JSON 回写 human_verdict 进数据集")
    parser.add_argument("--interactive", action="store_true", help="交互式逐条引导裁决，写 ratify.json")
    parser.add_argument("--ratify", type=Path, default=Path("workspace/evals/ratify.json"))
    parser.add_argument("--redo", action="store_true", help="交互模式重标已答项")
    parser.add_argument("--in-place", action="store_true", help="--apply 时原地更新数据集")
    parser.add_argument("--out", type=Path, help="--apply 后的数据集输出路径")
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if args.interactive:
        return interactive_label(dataset, args.ratify, redo=args.redo)
    if args.worksheet:
        n = write_worksheet(dataset, args.worksheet)
        print(f"已导出 {n} 例标注工作表: {args.worksheet}")
    if args.apply:
        ratify = json.loads(args.apply.read_text(encoding="utf-8"))
        applied = apply_labels(dataset, ratify)
        out = args.out or (args.dataset if args.in_place else None)
        if out is None:
            print(f"回写 {applied} 例；--in-place 或 --out 指定输出路径以持久化（未写盘）")
            return 0
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已回写 {applied}/{dataset.get('case_count')} 例 human_verdict → {out}")
    return 0


def _trunc(text: str, limit: int = 900) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n…（截断，共 {len(text)} 字）"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "（读不到）"


def interactive_label(dataset: dict[str, Any], ratify_path: Path, *, redo: bool = False) -> int:
    """交互式逐条引导：展示证据 → 记 human_verdict → 实时落盘 ratify.json（可续标）。"""
    import sys

    if not sys.stdin.isatty() and not redo:
        print("非交互终端：改用 --worksheet 导出静态工作表，或去掉重定向。")
        return 2

    source_root = Path(dataset.get("source_root", "."))
    wiki_root = Path(dataset.get("wiki_root", "."))
    ratify: dict[str, Any] = {}
    if ratify_path.is_file() and not redo:
        ratify = json.loads(ratify_path.read_text(encoding="utf-8"))

    cases = dataset.get("cases", [])
    reviewer_name = ""
    print(f"\n评测金标交互裁决：共 {len(cases)} 例（已答 {len(ratify)}，将续标）。")
    print("每例看证据后输入 p=pass / f=fail / r=review（Enter=pass）；可加备注：'f: 编造了X'。")
    print("命令：s=跳过 · q=退出(已存) · ?=帮助。金标=你对'这条编译结果该判什么'的裁决。\n")
    reviewer_name = input("标注人（reviewer，回车默认 you）: ").strip() or "you"

    def save() -> None:
        ratify_path.parent.mkdir(parents=True, exist_ok=True)
        ratify_path.write_text(
            json.dumps(ratify, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    for case in cases:
        cid = case["id"]
        if cid in ratify and not redo:
            continue
        _present(case, source_root, wiki_root)
        while True:
            raw = input(f"  [{cid}] verdict> ").strip()
            low = raw.lower()
            if low == "q":
                save()
                print(f"已存 {len(ratify)} 例到 {ratify_path}，续跑即从断点继续。")
                return 0
            if low == "s":
                break  # 跳过
            if low == "?":
                print("  p/f/r = pass/fail/review；带':'加备注；s 跳过；q 退出保存。")
                continue
            head, _, note = raw.partition(":")
            verdict = _ABBREV.get(head.strip().lower(), head.strip().lower())
            if verdict not in VALID:
                print("  只接受 p/f/r（或 pass/fail/review）；直接回车=pass。")
                continue
            ratify[cid] = {"human_verdict": verdict, "reviewer": reviewer_name, "notes": note.strip()}
            save()
            break
    save()
    print(f"\n✅ 已裁决 {len(ratify)}/{len(cases)} 例 → {ratify_path}")
    print("下一步：回写金标 make eval-apply-labels ；算一致率 make eval-agreement。")
    return 0


_TASK = (
    "任务：判断『这些 Wiki 页面对该 source 文档的编译结果』好不好——\n"
    "  事实讲全了吗？有没有编 source 里没有的东西？内容属实吗？\n"
    "  三问全 OK→p(pass) · 任一明显不 OK→f(fail) · 拿不准→r(review)\n"
    "  要看页面全文就另开终端： cat \"<wiki_root>/<路径>\"（judge 会逐论断核对，你只需给总体结论）"
)


def _present(case: dict[str, Any], source_root: Path, wiki_root: Path) -> None:
    print("─" * 70)
    print(f"■ {case['id']}  ·  {case['polarity']} / {case['suite']}  ·  source={case.get('source')}")
    if case["polarity"] == "negative":
        print("  这是『证据不足·应拒答』负例：下面论断【不该】由本 source 支撑，")
        print("  系统的正确行为是不产出/说证据不足。")
        for f in case.get("forbidden_claims", []):
            print(f"    ✗ 不该出现：{_trunc(f, 160)}")
        src = source_root / str(case.get("source", ""))
        print(f"  本 source 节选：{_trunc(_read(src), 260)}")
        print("  你只需确认：这是不是一条【有效】的应拒答负例？")
        print("    是（source 确实支撑不了上面论断）→ p ；你觉得其实能支撑（负例造错）→ f。")
        return
    print("  " + _TASK.replace("\n", "\n  "))
    print("  关键事实（来自 source，应被页面覆盖）：")
    for fact in case.get("required_facts", []):
        print(f"    · {_trunc(fact.get('assertion', ''), 150)}")
    pages = case.get("expected_pages", [])
    print(f"  待评 Wiki 页（{len(pages)} 个）：" + "、".join(pages) if pages else "  待评页：（无）")




if __name__ == "__main__":
    raise SystemExit(main())
