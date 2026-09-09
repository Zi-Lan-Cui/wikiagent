"""合并分层判定 A∧B 的离线单测：结构缺陷即使骗过语义 judge，也会被 A 层判 fail。"""

from __future__ import annotations

from pathlib import Path

from evals.core.adversarial import build_adversarial_dataset, score_adversarial_deterministic
from evals.core.judge_runner import combined_verdict


def _page(title: str) -> str:
    return (
        "---\ntype: concept\n"
        f'title: "{title}"\nsummary: "摘要内容"\ngoal: "目标"\nsources: ["s.md"]\n---\n\n'
        f"# {title}\n\n这是一段足够长的正文以满足质检。\n"
    )


def _base(tmp_path: Path) -> dict:
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "a.md").write_text(_page("A"), encoding="utf-8")
    (wiki / "concepts" / "b.md").write_text(_page("B"), encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "s.md").write_text("# s\n\n正文内容足够长以供抽取。\n", encoding="utf-8")
    positive = {
        "id": "p1", "task_type": "compile_outcome", "polarity": "positive", "suite": "capability",
        "source": "s.md", "source_identities": ["s.md"],
        "expected_pages": ["concepts/a.md", "concepts/b.md"],
        "required_facts": [{"id": "f1", "assertion": "正文内容足够长以供抽取。", "critical": True}],
        "forbidden_claims": [], "expected_behavior": {"allow_pages": True, "require_noop": False},
        "reference_solution": {"noop": False}, "expected_verdict": "pass",
    }
    return {"version": 3, "cases": [positive], "wiki_root": str(wiki), "source_root": str(src)}


def test_combined_verdict_truth_table():
    assert combined_verdict(True, "pass") == ("pass", True)
    assert combined_verdict(True, "fail") == ("fail", False)
    assert combined_verdict(False, "pass") == ("fail", False)
    assert combined_verdict(False, "fail") == ("fail", False)


def test_structural_defects_caught_even_if_judge_says_pass(tmp_path: Path):
    """核心牙齿：A 层抓住结构缺陷 → 合并判 fail，纵使 judge 被骗判 pass。"""
    adv = build_adversarial_dataset(_base(tmp_path), tmp_path / "wiki", per_kind=3)
    assert adv, "应生成对抗例"
    for case in adv:
        a_passed = not score_adversarial_deterministic(case)["caught"]
        if case["defect"] == "fabricate_claim":
            # 编造由 LLM judge 负责：A 层放过，合并结果取决于 judge（测 false-pass）
            assert a_passed is True
            assert combined_verdict(a_passed, "pass")[0] == "pass"
            assert combined_verdict(a_passed, "fail")[0] == "fail"
        else:
            # drop/strip/refuse/over_split/misroute：A 层抓住 ⇒ 合并必 fail，纵使 judge 被骗
            assert a_passed is False, f"{case['defect']} 应被 A 层兜住"
            assert combined_verdict(a_passed, "pass")[0] == "fail"
