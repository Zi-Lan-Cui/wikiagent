"""Outcome metric and private dataset builder tests."""

from __future__ import annotations

from evals.commands.build_dataset import build_dataset
from evals.core.outcomes import reliability_metrics, semantic_metrics


def test_semantic_metrics_keep_unknown_out_of_denominators() -> None:
    result = semantic_metrics(
        {
            "grounding": {
                "claims": [
                    {"verdict": "supported"},
                    {"verdict": "unsupported"},
                    {"verdict": "unknown"},
                ]
            },
            "coverage": {
                "facts": [
                    {"verdict": "covered"},
                    {"verdict": "covered"},
                    {"verdict": "missing"},
                ]
            },
        }
    )
    assert result["groundedness"] == 0.5
    assert result["coverage"] == 2 / 3
    assert result["unknown"] == 1


def test_reliability_requires_every_attempt_for_pass_power_k() -> None:
    result = reliability_metrics(
        [
            {"case_id": "a", "verdict": "pass"},
            {"case_id": "a", "verdict": "fail"},
            {"case_id": "b", "verdict": "pass"},
            {"case_id": "b", "verdict": "pass"},
        ]
    )
    assert result["pass_at_1"] == 0.75
    assert result["pass_power_k"] == 0.5


def test_dataset_builder_uses_verbatim_provenance_and_existing_pages(tmp_path) -> None:
    wiki = tmp_path / "wiki"
    provenance = tmp_path / "provenance"
    (wiki / "concepts").mkdir(parents=True)
    provenance.mkdir()
    for index in range(2):
        source = f"note-{index}.md"
        (wiki / "concepts" / f"page-{index}.md").write_text(
            "---\n"
            "type: concept\n"
            f'title: "Page {index}"\n'
            'summary: "long enough summary"\n'
            'goal: "long enough goal"\n'
            f'sources: ["{source}"]\n'
            "related: []\n"
            "---\n# Page\n\nBody content long enough for a page.",
            encoding="utf-8",
        )
        (provenance / source).write_text(
            "---\n"
            "type: source\n"
            f'title: "Note {index}"\n'
            f'sources: ["{source}"]\n'
            "---\n# Note\n\n"
            "这是第一条长度足够且可以直接核对来源的关键事实。\n"
            "这是第二条长度足够且可以直接核对来源的关键事实。",
            encoding="utf-8",
        )

    dataset = build_dataset(wiki, provenance, limit=2)
    assert dataset["case_count"] == 2
    assert all(case["expected_pages"] for case in dataset["cases"])
    assert all(len(case["required_facts"]) == 2 for case in dataset["cases"])
