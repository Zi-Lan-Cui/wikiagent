"""把评测结果渲染成可提交的报告（evals/reports/<date>/…）并汇总 HEADLINE.md。

报告含事实原文（语料可公开），manifest 记录 dataset 计数/重复次数/判定模型，使每
张表可复现。负例本轮为确定性证据（参考解过 no-op 评分器），不参与 snapshot LLM 判定。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(__file__).resolve().parents[2] / "evals" / "reports"


def today_slug() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def negative_evidence(dataset: dict[str, Any]) -> dict[str, Any]:
    """负例的确定性证据：数量 + 是否全部被标为 require_noop 且带禁语。"""
    neg = [c for c in dataset.get("cases", []) if c.get("polarity") == "negative"]
    return {
        "negative_count": len(neg),
        "all_require_noop": all(c.get("expected_behavior", {}).get("require_noop") for c in neg),
        "all_have_forbidden": all(c.get("forbidden_claims") for c in neg),
    }


def summarize_agreement(report: dict[str, Any]) -> dict[str, Any]:
    """从 agreement report 抽一致率与是否过 85% 门槛。"""
    cases = report.get("cases", [])
    graded = [c for c in cases if c.get("expected_verdict") and c.get("verdict")]
    agree = sum(c["expected_verdict"] == c["verdict"] for c in graded)
    rate = agree / len(graded) if graded else 0.0
    return {
        "labeled_cases": len(graded),
        "agreement": rate,
        "meets_85_gate": rate >= 0.85,
        "stats": report.get("summary", {}),
    }


def render_headline(date_str: str, parts: dict[str, Any], *, reports_dir: Path | None = None) -> Path:
    lines = [f"# 评测结果 HEADLINE · {date_str}", ""]
    ds = parts.get("dataset", {})
    lines += [
        "## 数据集",
        f"- 任务数：{ds.get('case_count', '?')}（正例 {ds.get('positive_count', '?')}"
        f" / 应拒答负例 {ds.get('negative_count', '?')}）",
        f"- 参考解确定性自检：{'全部通过' if parts.get('references_pass') else '存在失败'}",
    ]
    neg = parts.get("negatives")
    if neg:
        lines.append(
            f"- 负例：{neg['negative_count']} 条，全部 require_noop={neg['all_require_noop']}，"
            f"均带禁语={neg['all_have_forbidden']}"
        )
    agree = parts.get("agreement")
    if agree:
        lines += [
            "",
            "## LLM judge ↔ 人工一致率",
            f"- 已标注：{agree['labeled_cases']} 例，一致率 **{agree['agreement']:.1%}**，"
            f"{'≥85% ⇒ judge 可信' if agree['meets_85_gate'] else '<85% ⇒ 需继续校准 rubric'}",
            f"- precision/recall/F1：{agree['stats'].get('precision')}/"
            f"{agree['stats'].get('recall')}/{agree['stats'].get('f1')}",
        ]
    rel = parts.get("reliability")
    if rel:
        lines += [
            "",
            "## pass^k 可靠性（snapshot 模式）",
            f"- 用例 {rel.get('cases')} / 运行 {rel.get('attempts')}",
            f"- **pass@1 = {rel.get('pass_at_1'):.1%}**, pass^k(全过) = "
            f"{rel.get('pass_power_k'):.1%}",
        ]
    out = (reports_dir or REPORTS_DIR) / date_str
    out.mkdir(parents=True, exist_ok=True)
    (out / "HEADLINE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out / "HEADLINE.md"
