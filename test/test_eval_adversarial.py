"""对抗样本自检：注入的结构缺陷必须被 A 层抓住，编造缺陷故意放过（留给 judge）。

CI 内跑（无需 LLM/语料，用临时 wiki）。这证明评测有『牙齿』：评分器能识别坏输出，
而非只会给好输出盖章。
"""

from __future__ import annotations

from pathlib import Path

from evals.core.adversarial import (
    build_adversarial_dataset,
    score_adversarial_deterministic,
)


def _page(title: str) -> str:
    return (
        "---\n"
        "type: concept\n"
        f'title: "{title}"\n'
        'summary: "摘要内容"\n'
        'goal: "目标"\n'
        'sources: ["s.md"]\n'
        "---\n\n"
        f"# {title}\n\n"
        "这是一段足够长的正文，用于说明概念并满足质检的最低内容要求。\n"
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
        "forbidden_claims": [],
        "expected_behavior": {"allow_pages": True, "require_noop": False},
        "reference_solution": {"noop": False}, "expected_verdict": "pass",
    }
    negative = {
        "id": "n1", "task_type": "compile_outcome", "polarity": "negative", "suite": "regression",
        "source": "s.md", "source_identities": ["s.md"], "expected_pages": [],
        "required_facts": [], "forbidden_claims": ["完全无关的论断"],
        "expected_behavior": {"allow_pages": False, "require_noop": True},
        "reference_solution": {"noop": True}, "expected_verdict": "pass",
    }
    return {"version": 3, "cases": [positive, negative], "wiki_root": str(wiki), "source_root": str(src)}


def test_adversarial_structural_defects_are_caught(tmp_path: Path):
    base = _base(tmp_path)
    adv = build_adversarial_dataset(base, Path(base["wiki_root"]), per_kind=3)
    by_kind = {}
    for case in adv:
        by_kind.setdefault(case["defect"], []).append(case)
    # 结构性缺陷：全部应被 A 层抓住，且 gold=fail
    for kind in ("drop_page", "strip_provenance", "refuse_violation", "over_split", "misroute"):
        assert by_kind[kind], f"缺 {kind} 对抗例"
        for c in by_kind[kind]:
            assert c["human_verdict"] == "fail"
            assert score_adversarial_deterministic(c)["caught"] is True, f"{c['id']} 未被抓住"


def test_fabrication_is_blind_to_structural_gate(tmp_path: Path):
    base = _base(tmp_path)
    adv = build_adversarial_dataset(base, Path(base["wiki_root"]), per_kind=3)
    fab = [c for c in adv if c["defect"] == "fabricate_claim"]
    assert fab, "应生成 fabricate_claim 对抗例"
    for c in fab:
        # 编造由 LLM judge 负责，A 层结构门禁应放过（否则测不出 judge false-pass）
        assert score_adversarial_deterministic(c)["caught"] is False
