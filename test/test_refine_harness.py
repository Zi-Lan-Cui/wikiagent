import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.core.refine_harness import score_refine_run, summarize_refine


def test_refine_harness_accepts_self_update(tmp_path: Path):
    run = tmp_path / "run"
    folder = run / "artifacts" / "concepts_x"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(
        json.dumps(
            {
                "source": "concepts/x.md",
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    (folder / "page_before.md").write_text("old", encoding="utf-8")
    (folder / "page_after.md").write_text("new", encoding="utf-8")
    (folder / "plan.json").write_text(
        json.dumps(
            {
                "page_targets": [
                    {
                        "wiki_path": "concepts/x.md",
                        "disposition": "update",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = score_refine_run(run, tmp_path)[0]
    assert result.passed
    assert summarize_refine([result])["changed"] == 1


def test_refine_harness_rejects_cross_page_update(tmp_path: Path):
    run = tmp_path / "run"
    folder = run / "artifacts" / "concepts_x"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(
        json.dumps(
            {
                "source": "concepts/x.md",
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    (folder / "page_before.md").write_text("old", encoding="utf-8")
    (folder / "page_after.md").write_text("new", encoding="utf-8")
    (folder / "plan.json").write_text(
        json.dumps(
            {
                "page_targets": [
                    {
                        "wiki_path": "concepts/y.md",
                        "disposition": "update",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert not score_refine_run(run, tmp_path)[0].passed
