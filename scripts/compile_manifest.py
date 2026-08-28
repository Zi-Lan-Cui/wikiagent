#!/usr/bin/env python3
"""按 source manifest 分批编译 Wiki，并支持中断后恢复。

这个入口是评测/大批量导入的编排层，不改变 ``compile_folder`` 的语义：
每个 batch 仍然是一次独立的 scan + diff + Git commit。批次状态只记录在
Wiki 外部的 state 文件中，因此不会成为 Wiki 内容的一部分。

示例::

    uv run python scripts/compile_manifest.py \
      --root /media/zilan/.../notebook \
      --manifest evals/golden/source_manifest_120.json \
      --wiki-dir /tmp/wiki-agent-batched \
      --batch-size 20 --init-git

    uv run python scripts/compile_manifest.py ... --resume
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# compile_folder 自己也会补 src 路径；这里显式补路径是为了让本脚本既能
# 作为 scripts/compile_manifest.py 运行，也能被测试导入。
from scripts.compile_folder import compile_folder  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"manifest.sources 不能为空: {path}")
    ids: set[str] = set()
    for item in sources:
        if not isinstance(item, dict) or not item.get("id") or not item.get("path"):
            raise ValueError("每个 source 必须包含 id 和 path")
        if item["id"] in ids:
            raise ValueError(f"source id 重复: {item['id']}")
        ids.add(item["id"])
    return payload


def split_sources(sources: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    """按 manifest 原顺序切批；不按目录重排，便于结果复现。"""
    if batch_size <= 0:
        raise ValueError("batch-size 必须大于 0")
    return [sources[i : i + batch_size] for i in range(0, len(sources), batch_size)]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_head(wiki_dir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(wiki_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_git(wiki_dir: Path) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    if (wiki_dir / ".git").exists():
        return
    # compile_folder 的 run.log/events/run.json 在 Git commit 之后仍会继续
    # 更新；它们是审计产物，不应成为下一批 begin() 的 Wiki dirty 状态。
    # 只对本脚本新建的临时评测仓库写入，不修改用户已有仓库。
    (wiki_dir / ".gitignore").write_text(".logs/\n", encoding="utf-8")
    subprocess.run(["git", "init", str(wiki_dir)], check=True, capture_output=True, text=True)
    # 仅配置这个临时 Wiki 仓库，不触碰用户全局 Git 配置。
    subprocess.run(
        ["git", "-C", str(wiki_dir), "config", "user.email", "wiki-agent-batch@localhost"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_dir), "config", "user.name", "Wiki Agent Batch"], check=True
    )
    subprocess.run(["git", "-C", str(wiki_dir), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(wiki_dir), "commit", "--allow-empty", "-m", "wiki: batch baseline"],
        check=True,
        capture_output=True,
        text=True,
    )


def _materialize_batch(
    root: Path,
    sources: list[dict[str, Any]],
    batch_dir: Path,
) -> None:
    """将可能嵌套的笔记复制成 compile_folder 可读取的扁平目录。"""
    batch_dir.mkdir(parents=True, exist_ok=True)
    for item in sources:
        source = (root / item["path"]).resolve()
        if not source.is_file() or not source.is_relative_to(root):
            raise FileNotFoundError(f"source 不存在或越过 root: {item['path']}")
        target = batch_dir / f"{item['id']}__{source.name}"
        shutil.copy2(source, target)


def _failed_source_ids(run_dir: Path, sources: list[dict[str, Any]]) -> list[str]:
    """从现有事件流提取本批 ingest_failure，不另造失败真相源。"""
    event_file = run_dir / "events.jsonl"
    names = {f"{item['id']}__{Path(item['path']).name}": item["id"] for item in sources}
    failed: set[str] = set()
    if event_file.is_file():
        for line in event_file.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != "ingest_failure":
                continue
            filename = str(event.get("file", ""))
            if filename in names:
                failed.add(names[filename])
            else:
                # 事件字段在不同版本可能叫 source；保留可读记录，无法映射
                # 时不猜测 source id。
                source = str(event.get("source", ""))
                for name, source_id in names.items():
                    if source and source in name:
                        failed.add(source_id)
    return sorted(failed)


def _new_state(
    manifest: Path, root: Path, wiki_dir: Path, batch_size: int, batches: list[list[dict[str, Any]]]
) -> dict[str, Any]:
    return {
        "version": 1,
        "manifest": str(manifest.resolve()),
        "root": str(root.resolve()),
        "wiki_dir": str(wiki_dir.resolve()),
        "batch_size": batch_size,
        "created_at": _now(),
        "updated_at": _now(),
        "batches": [
            {
                "id": f"batch-{index:03d}",
                "source_ids": [item["id"] for item in batch],
                "status": "pending",
                "run_dir": None,
                "commit": None,
                "failed_source_ids": [],
                "error": None,
            }
            for index, batch in enumerate(batches, 1)
        ],
    }


def _validate_resume(state: dict[str, Any], manifest: Path, root: Path, wiki_dir: Path) -> None:
    expected = {
        "manifest": str(manifest.resolve()),
        "root": str(root.resolve()),
        "wiki_dir": str(wiki_dir.resolve()),
    }
    for key, value in expected.items():
        if state.get(key) != value:
            raise ValueError(
                f"resume 参数与 state 不一致: {key}: state={state.get(key)!r}, current={value!r}"
            )


async def run_batches(
    *,
    root: Path,
    manifest: Path,
    wiki_dir: Path,
    batch_size: int,
    state_path: Path,
    work_dir: Path,
    resume: bool = False,
    max_batches: int | None = None,
) -> dict[str, Any]:
    payload = load_manifest(manifest)
    root = root.expanduser().resolve()
    wiki_dir = wiki_dir.expanduser().resolve()
    batches = split_sources(payload["sources"], batch_size)

    if resume:
        if not state_path.is_file():
            raise FileNotFoundError(f"找不到 resume state: {state_path}")
        state = _read_json(state_path)
        _validate_resume(state, manifest, root, wiki_dir)
        if state.get("batch_size") != batch_size:
            raise ValueError("resume 时不能修改 batch-size")
    else:
        state = _new_state(manifest, root, wiki_dir, batch_size, batches)
        _write_json_atomic(state_path, state)

    if len(state.get("batches", [])) != len(batches):
        raise ValueError("manifest 的批次数量与 state 不一致")
    work_dir.mkdir(parents=True, exist_ok=True)

    processed = 0
    for index, (record, source_batch) in enumerate(zip(state["batches"], batches)):
        if max_batches is not None and processed >= max_batches:
            break
        if resume and record.get("status") == "committed":
            continue

        batch_dir = work_dir / record["id"] / "sources"
        _materialize_batch(root, source_batch, batch_dir)
        record.update({"status": "running", "started_at": _now(), "error": None})
        state["updated_at"] = _now()
        _write_json_atomic(state_path, state)
        try:
            run_dir = await compile_folder(batch_dir, wiki_dir=wiki_dir)
            run_record = _read_json(run_dir / "run.json")
            record["run_dir"] = str(run_dir)
            record["commit"] = run_record.get("commit")
            record["failed_source_ids"] = _failed_source_ids(run_dir, source_batch)
            record["finished_at"] = _now()
            if run_record.get("status") == "committed":
                record["status"] = "committed"
            else:
                record["status"] = "failed"
                record["error"] = f"run status: {run_record.get('status')}"
        except BaseException as exc:
            record.update(
                {
                    "status": "interrupted"
                    if isinstance(exc, asyncio.CancelledError)
                    else "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": _now(),
                }
            )
            state["updated_at"] = _now()
            _write_json_atomic(state_path, state)
            raise
        state["updated_at"] = _now()
        _write_json_atomic(state_path, state)
        processed += 1

    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="笔记根目录（manifest.path 相对于它）"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--state", type=Path, help="状态文件；默认写在 wiki-dir 的上级 .logs 外部")
    parser.add_argument("--work-dir", type=Path, help="批次临时 source 目录")
    parser.add_argument(
        "--resume", action="store_true", help="读取 state，跳过已经 committed 的批次"
    )
    parser.add_argument("--max-batches", type=int, help="最多执行几个批次，便于先做小规模试跑")
    parser.add_argument(
        "--init-git", action="store_true", help="wiki-dir 没有 Git 仓库时初始化临时仓库"
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    wiki_dir = args.wiki_dir.expanduser().resolve()
    if args.init_git:
        _init_git(wiki_dir)
    elif not (wiki_dir / ".git").exists():
        raise SystemExit("wiki-dir 不是 Git 仓库；临时评测请加 --init-git")
    state = args.state or (wiki_dir.parent / f"{wiki_dir.name}.batch_state.json")
    work_dir = args.work_dir or (wiki_dir.parent / f"{wiki_dir.name}.batch_sources")
    result = await run_batches(
        root=args.root,
        manifest=args.manifest,
        wiki_dir=wiki_dir,
        batch_size=args.batch_size,
        state_path=state,
        work_dir=work_dir,
        resume=args.resume,
        max_batches=args.max_batches,
    )
    counts: dict[str, int] = {}
    for batch in result["batches"]:
        counts[batch["status"]] = counts.get(batch["status"], 0) + 1
    print(json.dumps({"state": str(state), "batches": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parser().parse_args())))
