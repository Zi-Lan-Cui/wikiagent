"""Seeded-baseline corpus contracts and repository fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from evals.commands.reference_corpus import _tree_sha256, validate_corpus, validate_judgements

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "evals/corpora/reference-v1/manifest.json"


def test_repository_corpus_manifest_is_valid() -> None:
    manifest, errors = validate_corpus(MANIFEST_PATH)

    assert errors == []
    assert manifest is not None
    assert {b.id for b in manifest.baselines} == {"seeded-v1"}
    seeded = next(b for b in manifest.baselines if b.id == "seeded-v1")
    assert seeded.source_path == "baselines/seeded/source"
    assert seeded.source_sha256 is not None
    assert seeded.wiki_sha256 is not None


def test_judgements_cover_all_four_dimensions() -> None:
    errors = validate_judgements(MANIFEST_PATH)
    assert errors == []
    gold = json.loads(
        (MANIFEST_PATH.parent / "verdicts" / "judgements-v2.json").read_text(encoding="utf-8")
    )
    cases = gold["cases"]
    dimensions = {case["dimension"] for case in cases}
    assert dimensions == {"grounding", "coverage", "organization", "uncertainty"}
    # 真值混合：不是全部正例也不是全部负例
    golds = [case["gold"] for case in cases]
    assert any(golds) and not all(golds)
    # 每题证据与理由非空
    for case in cases:
        assert case["evidence"].strip()
        assert case["reason"].strip()
        assert case["claim"].strip()


def test_judgement_pages_and_sources_exist() -> None:
    root = MANIFEST_PATH.parent
    gold = json.loads(
        (root / "verdicts" / "judgements-v2.json").read_text(encoding="utf-8")
    )
    for case in gold["cases"]:
        assert (root / "baselines" / "seeded" / "source" / case["source"]).is_file()
        for page in case["target"].get("pages", []):
            assert (root / "baselines" / "seeded" / "wiki" / page).is_file(), page


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


def test_repository_seeded_baseline_has_six_locked_notes() -> None:
    manifest, errors = validate_corpus(MANIFEST_PATH)
    assert errors == []
    assert manifest is not None
    seeded = next(b for b in manifest.baselines if b.id == "seeded-v1")
    source_dir = MANIFEST_PATH.parent / seeded.source_path
    assert source_dir.is_dir()
    notes = sorted(p.name for p in source_dir.glob("*.md"))
    assert notes == [
        "baseline-cache.md",
        "baseline-jobs.md",
        "baseline-lock.md",
        "baseline-logging.md",
        "baseline-migration.md",
        "baseline-retry.md",
        "note-circuit-breaker.md",
        "note-idempotency-design.md",
        "note-message-delivery.md",
        "note-rate-limiting.md",
        "note-saga-compensation.md",
        "note-timeout-degradation.md",
    ]
    for note in source_dir.glob("*.md"):
        assert len(note.read_text(encoding="utf-8").strip()) >= 700
