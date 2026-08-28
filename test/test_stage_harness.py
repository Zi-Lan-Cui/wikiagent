import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.core.harness import GoldenCase
from evals.core.stage_harness import score_run, summarize


def _case() -> GoldenCase:
    return GoldenCase(
        id="case",
        cluster="cluster",
        source="notes/input.md",
        sha256="",
        must_include=["fact"],
        must_not_invent=[],
    )


def test_stage_harness_scores_valid_chain(tmp_path: Path):
    run = tmp_path / "run"
    folder = run / "artifacts" / "input.md"
    folder.mkdir(parents=True)
    (tmp_path / "concepts").mkdir()
    (tmp_path / "concepts" / "known.md").write_text("# known", encoding="utf-8")
    (folder / "extract.json").write_text("fact", encoding="utf-8")
    (folder / "search.json").write_text(
        json.dumps(
            {
                "source": "input.md",
                "rel_paths": ["concepts/known.md"],
                "raw": "[]",
            }
        ),
        encoding="utf-8",
    )
    (folder / "analyze.json").write_text(
        json.dumps(
            {
                "source": "input.md",
                "raw": "{}",
                "entities": [],
                "concepts": [],
                "relationships": [
                    {
                        "from_page": "input.md",
                        "to_page": "concepts/known.md",
                        "relation": "extends",
                        "detail": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (folder / "plan.json").write_text(
        json.dumps(
            {
                "page_targets": [
                    {
                        "wiki_path": "concepts/new.md",
                        "title": "new",
                        "disposition": "new",
                        "reason": "补充事实",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = score_run(run, tmp_path, [_case()])[0]
    assert result.search.passed
    assert result.analyze.passed
    assert result.plan.passed
    assert summarize([result])["plan"]["pass_rate"] == 1.0


def test_stage_harness_rejects_ghost_and_duplicate_targets(tmp_path: Path):
    run = tmp_path / "run"
    folder = run / "artifacts" / "input.md"
    folder.mkdir(parents=True)
    (folder / "extract.json").write_text("fact", encoding="utf-8")
    (folder / "search.json").write_text(
        json.dumps(
            {
                "rel_paths": ["concepts/missing.md", "concepts/missing.md"],
                "raw": "[]",
            }
        ),
        encoding="utf-8",
    )
    (folder / "analyze.json").write_text("{}", encoding="utf-8")
    (folder / "plan.json").write_text(
        json.dumps(
            {
                "page_targets": [
                    {"wiki_path": "wiki/bad.md", "title": "", "disposition": "bad", "reason": ""},
                    {"wiki_path": "wiki/bad.md", "title": "", "disposition": "bad", "reason": ""},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = score_run(run, tmp_path, [_case()])[0]
    assert not result.search.passed
    assert not result.plan.passed
