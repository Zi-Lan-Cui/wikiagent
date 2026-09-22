"""Manifest 薄编排——materialize 扁平化 + 逐批 sync 委托；无第二本账。"""

import asyncio
import json
from pathlib import Path

import pytest

from wiki_agent.application import compile_batches


def _manifest(tmp_path: Path, ids: list[str]) -> Path:
    root = tmp_path / "notes"
    root.mkdir(parents=True, exist_ok=True)
    sources = []
    for name in ids:
        (root / f"{name}.md").write_text(f"# {name} 内容", encoding="utf-8")
        sources.append({"id": name, "path": f"{name}.md"})
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    return path


def test_split_sources_is_stable_and_validates_size():
    sources = [{"id": f"source-{i:03d}", "path": f"{i}.md"} for i in range(5)]
    batches = compile_batches.split_sources(sources, 2)
    assert [[item["id"] for item in batch] for batch in batches] == [
        ["source-000", "source-001"],
        ["source-002", "source-003"],
        ["source-004"],
    ]
    with pytest.raises(ValueError):
        compile_batches.split_sources(sources, 0)


def test_load_manifest_validates_ids_and_paths(tmp_path: Path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"sources": [{"id": "a", "path": "a.md"}]}), encoding="utf-8")
    assert compile_batches.load_manifest(path)["sources"][0]["id"] == "a"
    path.write_text(json.dumps({"sources": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="不能为空"):
        compile_batches.load_manifest(path)
    path.write_text(json.dumps({"sources": [{"id": "a"}, {"id": "a"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        compile_batches.load_manifest(path)


def test_materialize_flattens_with_id_prefix(tmp_path: Path):
    root = tmp_path / "notes"
    (root / "deep").mkdir(parents=True)
    (root / "deep" / "n.md").write_text("嵌套笔记", encoding="utf-8")
    batch_dir = tmp_path / "staging" / "b0"
    compile_batches.materialize_batch(
        root, [{"id": "source-001", "path": "deep/n.md"}], batch_dir
    )
    assert [p.name for p in batch_dir.iterdir()] == ["source-001__n.md"]
    # 越界防护
    (root / "up.md").write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        compile_batches.materialize_batch(
            root, [{"id": "s", "path": "../up.md"}], tmp_path / "staging" / "b1"
        )


def test_materialize_clears_stale_files_for_deterministic_digest(tmp_path: Path):
    """staging 路径即账本键——重跑先清场，避免陈旧变体污染快照差集。"""
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    (root / "a.md").write_text("甲", encoding="utf-8")
    batch_dir = tmp_path / "staging" / "b0"
    batch_dir.mkdir(parents=True)
    (batch_dir / "stale--旧变体.md").write_text("残留", encoding="utf-8")
    compile_batches.materialize_batch(root, [{"id": "s1", "path": "a.md"}], batch_dir)
    assert [p.name for p in batch_dir.iterdir()] == ["s1__a.md"]


def test_run_manifest_delegates_each_batch_to_sync(tmp_path: Path):
    sources = [f"source-{i:03d}" for i in range(3)]
    manifest = _manifest(tmp_path, sources)
    seen: list[list[str]] = []

    async def fake_execute(batch_dir: Path) -> int:
        names = sorted(p.name for p in batch_dir.iterdir())
        seen.append(names)
        return len(names)

    result = asyncio.run(
        compile_batches.run_manifest(
            root=tmp_path / "notes",
            manifest=manifest,
            wiki_dir=tmp_path / "wiki",
            workspace=tmp_path / "ws",
            work_dir=tmp_path / "staging",
            batch_size=2,
            execute=fake_execute,
        )
    )
    assert result == {"batches_run": 2, "batches_total": 2, "enqueued": 3}
    assert seen == [
        ["source-000__source-000.md", "source-001__source-001.md"],
        ["source-002__source-002.md"],
    ]
    # 编排不落状态文件——进度只有 sync 账本与队列
    assert sorted(p.name for p in (tmp_path / "staging").iterdir()) == ["batch-000", "batch-001"]
    assert not (tmp_path / "ws").exists()  # 假 execute 不碰 workspace


def test_run_manifest_respects_max_batches(tmp_path: Path):
    manifest = _manifest(tmp_path, [f"s{i}" for i in range(5)])
    calls = []

    async def fake_execute(batch_dir: Path) -> int:
        calls.append(batch_dir.name)
        return 1

    result = asyncio.run(
        compile_batches.run_manifest(
            root=tmp_path / "notes",
            manifest=manifest,
            wiki_dir=tmp_path / "wiki",
            workspace=tmp_path / "ws",
            work_dir=tmp_path / "staging",
            batch_size=2,
            max_batches=1,
            execute=fake_execute,
        )
    )
    assert calls == ["batch-000"]
    assert result["batches_run"] == 1 and result["batches_total"] == 3


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        try:
            fn(Path(tempfile.mkdtemp()))
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
