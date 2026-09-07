import asyncio
import json
from pathlib import Path

import pytest

from wiki_agent.application import batch_compile as compile_manifest


def test_split_sources_is_stable_and_validates_size():
    sources = [{"id": f"source-{i:03d}", "path": f"{i}.md"} for i in range(5)]
    batches = compile_manifest.split_sources(sources, 2)
    assert [[item["id"] for item in batch] for batch in batches] == [
        ["source-000", "source-001"],
        ["source-002", "source-003"],
        ["source-004"],
    ]
    with pytest.raises(ValueError):
        compile_manifest.split_sources(sources, 0)


def test_run_batches_records_commit_and_resume_skips_committed(tmp_path: Path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    sources = []
    for index in range(3):
        name = f"note-{index}.md"
        (root / name).write_text(f"# note {index}", encoding="utf-8")
        sources.append({"id": f"source-{index:03d}", "path": name})
    manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")

    calls: list[list[str]] = []

    async def fake_compile(source_dir: Path, *, wiki_dir: Path, source_checkpoint=None):
        copied = sorted(path.name for path in Path(source_dir).iterdir())
        calls.append(copied)
        run_dir = wiki_dir / ".logs" / "runs" / f"run-{len(calls)}"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "status": "committed",
                    "commit": f"commit-{len(calls)}",
                }
            ),
            encoding="utf-8",
        )
        return run_dir

    monkeypatch.setattr(compile_manifest, "compile_sources", fake_compile)
    state_path = tmp_path / "state.json"
    work_dir = tmp_path / "work"

    first = asyncio.run(
        compile_manifest.run_batches(
            root=root,
            manifest=manifest,
            wiki_dir=tmp_path / "wiki",
            batch_size=2,
            state_path=state_path,
            work_dir=work_dir,
        )
    )
    assert [batch["status"] for batch in first["batches"]] == [
        "committed",
        "committed",
    ]
    assert len(calls) == 2

    second = asyncio.run(
        compile_manifest.run_batches(
            root=root,
            manifest=manifest,
            wiki_dir=tmp_path / "wiki",
            batch_size=2,
            state_path=state_path,
            work_dir=work_dir,
            resume=True,
        )
    )
    assert len(calls) == 2
    assert second["batches"][0]["commit"] == "commit-1"


def test_run_batches_persists_failure_before_propagating(tmp_path: Path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    source = root / "note.md"
    source.write_text("# note", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"sources": [{"id": "source-001", "path": "note.md"}]}), encoding="utf-8"
    )

    async def failing_compile(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(compile_manifest, "compile_sources", failing_compile)
    state_path = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="simulated failure"):
        asyncio.run(
            compile_manifest.run_batches(
                root=root,
                manifest=manifest,
                wiki_dir=tmp_path / "wiki",
                batch_size=1,
                state_path=state_path,
                work_dir=tmp_path / "work",
            )
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["batches"][0]["status"] == "failed"
    assert "simulated failure" in state["batches"][0]["error"]


def test_run_with_failed_source_is_not_marked_committed(tmp_path: Path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    source = root / "note.md"
    source.write_text("# note", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"sources": [{"id": "source-001", "path": "note.md"}]}), encoding="utf-8"
    )

    async def fake_compile(source_dir: Path, *, wiki_dir: Path, source_checkpoint=None):
        run_dir = wiki_dir / ".logs" / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({"status": "committed"}), encoding="utf-8")
        (run_dir / "events.jsonl").write_text(
            json.dumps({"event": "ingest_failure", "file": "source-001__note.md"}) + "\n",
            encoding="utf-8",
        )
        return run_dir

    monkeypatch.setattr(compile_manifest, "compile_sources", fake_compile)
    state = asyncio.run(
        compile_manifest.run_batches(
            root=root,
            manifest=manifest,
            wiki_dir=tmp_path / "wiki",
            batch_size=1,
            state_path=tmp_path / "state.json",
            work_dir=tmp_path / "work",
        )
    )
    assert state["batches"][0]["status"] == "failed"
    assert state["source_state"]["source-001"]["status"] == "failed"


def test_resume_rejects_changed_source_content(tmp_path: Path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    source = root / "note.md"
    source.write_text("before", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"sources": [{"id": "source-001", "path": "note.md"}]}),
        encoding="utf-8",
    )

    async def fake_compile(source_dir: Path, *, wiki_dir: Path, source_checkpoint=None):
        run_dir = wiki_dir / ".logs" / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({"status": "committed"}), encoding="utf-8")
        return run_dir

    monkeypatch.setattr(compile_manifest, "compile_sources", fake_compile)
    state_path = tmp_path / "state.json"
    asyncio.run(
        compile_manifest.run_batches(
            root=root,
            manifest=manifest,
            wiki_dir=tmp_path / "wiki",
            batch_size=1,
            state_path=state_path,
            work_dir=tmp_path / "work",
        )
    )
    source.write_text("after", encoding="utf-8")

    with pytest.raises(ValueError, match="source 内容或路径已变化"):
        asyncio.run(
            compile_manifest.run_batches(
                root=root,
                manifest=manifest,
                wiki_dir=tmp_path / "wiki",
                batch_size=1,
                state_path=state_path,
                work_dir=tmp_path / "work",
                resume=True,
            )
        )


def test_reconcile_rebuilds_batches_and_preserves_unchanged_sources(tmp_path: Path):
    root = tmp_path / "notes"
    root.mkdir()
    first = root / "first.md"
    second = root / "second.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    sources = [
        {"id": "source-001", "path": "first.md"},
        {"id": "source-002", "path": "second.md"},
    ]
    manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    batches = compile_manifest.split_sources(sources, 1)
    state = compile_manifest._new_state(
        manifest, root, tmp_path / "wiki", 1, batches, compile_manifest._sha256(manifest)
    )
    state["source_state"]["source-001"]["status"] = "committed"
    new = root / "new.md"
    new.write_text("new", encoding="utf-8")
    sources.append({"id": "source-003", "path": "new.md"})
    manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")

    reconciled = compile_manifest._reconcile_state(
        state,
        manifest=manifest,
        root=root,
        wiki_dir=tmp_path / "wiki",
        batch_size=2,
        batches=compile_manifest.split_sources(sources, 2),
        manifest_hash=compile_manifest._sha256(manifest),
    )

    assert reconciled["source_state"]["source-001"]["status"] == "committed"
    assert reconciled["source_state"]["source-003"]["status"] == "pending"
    assert [batch["source_ids"] for batch in reconciled["batches"]] == [
        ["source-001", "source-002"],
        ["source-003"],
    ]
