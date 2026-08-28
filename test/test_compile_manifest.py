import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import compile_manifest


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

    async def fake_compile(source_dir: Path, *, wiki_dir: Path):
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

    monkeypatch.setattr(compile_manifest, "compile_folder", fake_compile)
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

    monkeypatch.setattr(compile_manifest, "compile_folder", failing_compile)
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
