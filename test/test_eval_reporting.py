"""评测报告/标注工具链的离线单测（不碰 LLM、不污染仓库 reports 目录）。"""

from __future__ import annotations

from pathlib import Path

from evals.commands import report as report_cmd
from evals.commands.label import apply_labels, write_worksheet
from evals.core import reporting


def _dataset() -> dict:
    return {
        "version": 3,
        "dataset_id": "t",
        "case_count": 2,
        "positive_count": 1,
        "negative_count": 1,
        "wiki_root": "/nowhere",
        "cases": [
            {
                "id": "p1",
                "task_type": "compile_outcome",
                "polarity": "positive",
                "suite": "capability",
                "source": "a.md",
                "source_identities": ["a.md"],
                "expected_pages": ["concepts/x.md"],
                "required_facts": [{"id": "f1", "assertion": "A", "critical": True}],
                "forbidden_claims": [],
                "expected_behavior": {"allow_pages": True, "require_noop": False},
                "reference_solution": {"noop": False},
                "expected_verdict": "pass",
            },
            {
                "id": "n1",
                "task_type": "compile_outcome",
                "polarity": "negative",
                "suite": "regression",
                "source": "a.md",
                "source_identities": ["a.md"],
                "expected_pages": [],
                "required_facts": [],
                "forbidden_claims": ["B"],
                "expected_behavior": {"allow_pages": False, "require_noop": True},
                "reference_solution": {"noop": True},
                "expected_verdict": "pass",
            },
        ],
    }


def test_label_worksheet_and_apply_roundtrip(tmp_path: Path):
    ds = _dataset()
    ws = tmp_path / "ws.md"
    assert write_worksheet(ds, ws) == 2
    text = ws.read_text(encoding="utf-8")
    assert "## p1" in text and "human_verdict" in text
    applied = apply_labels(ds, {"p1": {"human_verdict": "pass", "reviewer": "z"}})
    assert applied == 1
    assert ds["cases"][0]["human_verdict"] == "pass"
    assert ds["cases"][0]["annotation"]["review_status"] == "human_ratified"


def test_apply_rejects_bad_verdict():
    import pytest

    with pytest.raises(ValueError):
        apply_labels(_dataset(), {"p1": {"human_verdict": "maybe", "reviewer": "z"}})


def test_negative_evidence():
    ev = reporting.negative_evidence(_dataset())
    assert ev["negative_count"] == 1
    assert ev["all_require_noop"] and ev["all_have_forbidden"]


def test_summarize_agreement_gate():
    payload = {
        "cases": [
            {"verdict": "pass", "expected_verdict": "pass"},
            {"verdict": "pass", "expected_verdict": "pass"},
            {"verdict": "fail", "expected_verdict": "pass"},
        ]
    }
    agree = reporting.summarize_agreement(payload)
    assert agree["labeled_cases"] == 3
    assert abs(agree["agreement"] - 2 / 3) < 1e-9
    assert agree["meets_85_gate"] is False


def test_render_headline_writes_file(tmp_path: Path):
    parts = {
        "dataset": _dataset(),
        "references_pass": True,
        "negatives": reporting.negative_evidence(_dataset()),
    }
    path = reporting.render_headline("2026-09-09", parts, reports_dir=tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "HEADLINE · 2026-09-09" in content and "参考解确定性自检：全部通过" in content


def test_build_parts_references_pass(tmp_path: Path):
    # 只有负例时不需要 wiki，参考解自检应通过（负例走 score_refusal，天然 no-op）
    ds = {"version": 3, "cases": [_dataset()["cases"][1]]}
    parts = report_cmd.build_parts(ds, tmp_path)
    assert parts["references_pass"] is True
    assert parts["schema_errors"] == []
