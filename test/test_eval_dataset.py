"""A 层确定性评分器 + 参考解自检的单元测试（评分器自身要被测，不靠 LLM）。"""

from __future__ import annotations

from pathlib import Path

from evals.core.dataset import (
    assert_all_references_pass,
    run_reference_graders,
    score_refusal,
    validate_schema_v3,
)

_PAGE = (
    "---\n"
    "type: concept\n"
    'title: "Lambda"\n'
    'summary: "一种匿名函数"\n'
    'goal: "解释 lambda"\n'
    'sources: ["a.md"]\n'
    "---\n\n"
    "# Lambda\n\n"
    "Lambda 是匿名函数，用于短小回调。\n"
)

_FACT = "Lambda 是匿名函数，用于短小回调。"


def _dataset() -> dict:
    return {
        "version": 3,
        "cases": [
            {
                "id": "wiki-001",
                "task_type": "compile_outcome",
                "suite": "capability",
                "polarity": "positive",
                "source": "a.md",
                "sha256": "x",
                "source_identities": ["a.md"],
                "expected_pages": ["concepts/lambda.md"],
                "required_facts": [{"id": "f1", "assertion": _FACT, "critical": True}],
                "forbidden_claims": [],
                "expected_behavior": {"allow_pages": True, "require_noop": False},
                "reference_solution": {"noop": False},
                "expected_verdict": "pass",
            },
            {
                "id": "wiki-neg-001",
                "task_type": "compile_outcome",
                "suite": "regression",
                "polarity": "negative",
                "source": "a.md",
                "sha256": "y",
                "source_identities": ["a.md"],
                "expected_pages": [],
                "required_facts": [],
                "forbidden_claims": ["外部事实"],
                "expected_behavior": {"allow_pages": False, "require_noop": True},
                "reference_solution": {"noop": True, "abstain": True},
                "expected_verdict": "pass",
            },
        ],
    }


def _wiki(tmp_path: Path) -> Path:
    (tmp_path / "concepts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "concepts" / "lambda.md").write_text(_PAGE, encoding="utf-8")
    return tmp_path


def test_valid_schema_has_no_errors():
    assert validate_schema_v3(_dataset()) == []


def test_schema_flags_missing_reference_and_bad_enums():
    ds = _dataset()
    del ds["cases"][0]["reference_solution"]
    ds["cases"][0]["polarity"] = "maybe"
    ds["version"] = 2
    errors = validate_schema_v3(ds)
    assert any("reference_solution" in e for e in errors)
    assert any("polarity" in e for e in errors)
    assert any("version" in e for e in errors)


def test_reference_solutions_pass_deterministic_graders(tmp_path: Path):
    wiki = _wiki(tmp_path)
    assert assert_all_references_pass(_dataset(), wiki) == []


def test_positive_reference_rejects_broken_provenance(tmp_path: Path):
    # 页缺指定来源 → 来源同一性(authority)腿应判失败
    wiki = tmp_path
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "lambda.md").write_text(
        _PAGE.replace('sources: ["a.md"]', 'sources: ["other.md"]'), encoding="utf-8"
    )
    ds = _dataset()
    result = run_reference_graders(ds["cases"][0], wiki)
    assert result["passed"] is False
    assert any("未引用任何指定来源身份" in e["reason"] for e in result["checks"]["integrity"]["errors"])


def test_refusal_grader_passes_on_noop():
    case = _dataset()["cases"][1]
    result = score_refusal(case, produced_paths=set(), rendered_text="")
    assert result["passed"] is True


def test_refusal_grader_rejects_invention():
    case = _dataset()["cases"][1]
    produced = score_refusal(case, produced_paths={"concepts/ghost.md"}, rendered_text="")
    assert produced["passed"] is False and produced["invented_pages"] == ["concepts/ghost.md"]
    hit = score_refusal(case, produced_paths=set(), rendered_text="含外部事实的编造")
    assert hit["passed"] is False and hit["forbidden_hits"] == ["外部事实"]
