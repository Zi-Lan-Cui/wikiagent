"""semantic eval runner 的 manifest/artifact 映射测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run_semantic_eval import (
    _artifact_folder,
    _load_cases,
    _pages_from_plan,
)


def test_load_source_manifest_as_cases():
    cases, is_source_manifest = _load_cases(Path("evals/golden/source_manifest_120.json"))

    assert is_source_manifest is True
    assert len(cases) == 120
    assert cases[0]["id"] == "source-001"
    assert cases[0]["source"]
    assert cases[0]["cluster"]["expected_pages"] == []


def test_artifact_folder_resolves_source_id_prefix(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    (artifacts / "source-001__中文文件.md").mkdir(parents=True)
    case = {"id": "source-001", "source": "原目录/中文文件.md"}

    assert _artifact_folder(tmp_path, case).name == "source-001__中文文件.md"


def test_artifact_folder_searches_multiple_batch_runs(tmp_path: Path):
    first = tmp_path / "batch-001"
    second = tmp_path / "batch-002"
    (second / "artifacts" / "source-021__第二批.md").mkdir(parents=True)
    case = {"id": "source-021", "source": "第二批.md"}

    assert _artifact_folder([first, second], case).name == "source-021__第二批.md"


def test_pages_from_plan_reads_final_wiki_page(tmp_path: Path):
    folder = tmp_path / "artifacts"
    folder.mkdir()
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    page = wiki / "concepts" / "demo.md"
    page.write_text("---\ntitle: Demo\n---\n# Demo\n\n正文", encoding="utf-8")

    pages = _pages_from_plan(folder, wiki, {"page_targets": [{"wiki_path": "concepts/demo.md"}]})

    assert pages == [{"path": "concepts/demo.md", "content": page.read_text()}]
