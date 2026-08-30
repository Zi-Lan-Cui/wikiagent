"""Persistent state and input reconciliation for resumable compiler runs."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    temp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_snapshot(root: Path, item: dict[str, Any]) -> dict[str, str]:
    source = (root / item["path"]).resolve()
    if not source.is_file() or not source.is_relative_to(root):
        raise ValueError(f"source 不存在或越过 root: {item['id']} ({item['path']})")
    return {
        "id": str(item["id"]),
        "path": str(item["path"]),
        "sha256": sha256(source),
        "size": str(source.stat().st_size),
    }


def new_state(
    manifest: Path,
    root: Path,
    wiki_dir: Path,
    batch_size: int,
    batches: list[list[dict[str, Any]]],
    manifest_hash: str,
    commit_scope: str = "batch",
) -> dict[str, Any]:
    snapshots = [source_snapshot(root, item) for batch in batches for item in batch]
    return {
        "version": 1,
        "manifest": str(manifest.resolve()),
        "manifest_hash": manifest_hash,
        "root": str(root.resolve()),
        "wiki_dir": str(wiki_dir.resolve()),
        "batch_size": batch_size,
        "commit_scope": commit_scope,
        "source_state": {
            snapshot["id"]: {
                **snapshot,
                "status": "pending",
                "completed_stage": "",
                "error": None,
            }
            for snapshot in snapshots
        },
        "created_at": now(),
        "updated_at": now(),
        "batches": [
            {
                "id": f"batch-{index:03d}",
                "source_ids": [item["id"] for item in batch],
                "sources": [source_snapshot(root, item) for item in batch],
                "status": "pending",
                "run_dir": None,
                "commit": None,
                "failed_source_ids": [],
                "error": None,
            }
            for index, batch in enumerate(batches, 1)
        ],
    }


def validate_resume(
    state: dict[str, Any],
    manifest: Path,
    root: Path,
    wiki_dir: Path,
    batches: list[list[dict[str, Any]]],
    manifest_hash: str,
) -> None:
    for key, value in {
        "manifest": str(manifest.resolve()),
        "root": str(root.resolve()),
        "wiki_dir": str(wiki_dir.resolve()),
    }.items():
        if state.get(key) != value:
            raise ValueError(
                f"resume 参数与 state 不一致: {key}: state={state.get(key)!r}, current={value!r}"
            )
    expected = {
        snapshot["id"]: snapshot
        for batch in state.get("batches", [])
        for snapshot in batch.get("sources", [])
    }
    current = {
        snapshot["id"]: snapshot
        for batch in batches
        for item in batch
        for snapshot in [source_snapshot(root, item)]
    }
    if set(expected) != set(current):
        added = sorted(set(current) - set(expected))
        removed = sorted(set(expected) - set(current))
        raise ValueError(f"source 列表已变化，拒绝 resume；新增={added}, 删除={removed}")
    changed = [
        source_id for source_id in sorted(expected) if expected[source_id] != current[source_id]
    ]
    if changed:
        raise ValueError(f"source 内容或路径已变化，拒绝 resume；变更={changed}")
    if state.get("manifest_hash") != manifest_hash:
        raise ValueError("manifest 内容已变化，拒绝 resume；请新建运行或显式执行 reconcile")


def reconcile(
    state: dict[str, Any],
    *,
    manifest: Path,
    root: Path,
    wiki_dir: Path,
    batch_size: int,
    batches: list[list[dict[str, Any]]],
    manifest_hash: str,
    commit_scope: str = "batch",
) -> dict[str, Any]:
    previous = state.get("source_state", {})
    snapshots = [source_snapshot(root, item) for batch in batches for item in batch]
    source_state: dict[str, Any] = {}
    for snapshot in snapshots:
        old = previous.get(snapshot["id"], {})
        unchanged = old.get("sha256") == snapshot["sha256"] and old.get("path") == snapshot["path"]
        keep_done = unchanged and old.get("status") == "committed"
        source_state[snapshot["id"]] = {
            **snapshot,
            "status": "committed" if keep_done else "pending",
            "completed_stage": old.get("completed_stage", "") if keep_done else "",
            "error": None if keep_done else ("source changed" if old else None),
        }
    new_batches = []
    for index, batch in enumerate(batches, 1):
        ids = [str(item["id"]) for item in batch]
        statuses = [source_state[source_id]["status"] for source_id in ids]
        new_batches.append(
            {
                "id": f"batch-{index:03d}",
                "source_ids": ids,
                "sources": [source_state[source_id] for source_id in ids],
                "status": "committed"
                if statuses and all(status == "committed" for status in statuses)
                else "pending",
                "run_dir": None,
                "commit": None,
                "failed_source_ids": [],
                "error": None,
            }
        )
    state.update(
        {
            "manifest": str(manifest.resolve()),
            "manifest_hash": manifest_hash,
            "root": str(root.resolve()),
            "wiki_dir": str(wiki_dir.resolve()),
            "batch_size": batch_size,
            "commit_scope": commit_scope,
            "source_state": source_state,
            "batches": new_batches,
            "updated_at": now(),
        }
    )
    return state
