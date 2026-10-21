"""manifest 契约与确定性校验的纯逻辑测试——只喂 tmp，不读任何真实语料（dev/test 已 gitignore）。"""

from __future__ import annotations

import json
from pathlib import Path

from evals.commands.reference_corpus import _tree_sha256, validate_corpus, validate_grader


def test_validate_grader_clean_on_repo_file() -> None:
    assert validate_grader() == []


def test_validator_detects_source_drift(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("内容", encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "corpus_id": "drift-test",
        "description": "test",
        "baselines": [
            {
                "id": "seed",
                "wiki_path": "wiki",
                "wiki_sha256": _tree_sha256(wiki),
                "source_path": "source",
                "source_sha256": "0" * 64,
                "description": "test",
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _, errors = validate_corpus(manifest_path)

    assert errors == ["baseline seed: source SHA-256 不匹配"]


def test_validator_rejects_missing_baseline_dir(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 2,
        "corpus_id": "c",
        "description": "d",
        "baselines": [{"id": "b", "wiki_path": "nope", "description": "t"}],
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    _, errors = validate_corpus(p)
    assert any("目录不存在" in e for e in errors)
