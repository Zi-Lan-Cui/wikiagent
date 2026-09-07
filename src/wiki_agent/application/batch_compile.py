"""按 source manifest 分批编译 Wiki，并支持中断后恢复。

这个入口是评测/大批量导入的编排层，不改变 ``compile_sources`` 的语义：
每个 batch 仍然是一次独立的 scan + diff + Git commit。批次状态只记录在
Wiki 外部的 state 文件中，因此不会成为 Wiki 内容的一部分。

示例::

    uv run python scripts/compile_manifest.py \
      --root /path/to/notebook \
      --manifest evals/templates/source_manifest.json \
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
from pathlib import Path
from typing import Any

from wiki_agent.application.compile_service import compile_sources
from wiki_agent.compiler.workflows import run_state as _run_state
from wiki_agent.config import load_config

_new_state = _run_state.new_state
_now = _run_state.now
_read_json = _run_state.read_json
_reconcile_state = _run_state.reconcile
_sha256 = _run_state.sha256
_validate_resume = _run_state.validate_resume
_write_json_atomic = _run_state.write_json_atomic


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
    subprocess.run(["git", "init", str(wiki_dir)], check=True, capture_output=True, text=True)
    # 仅配置这个临时 Wiki 仓库，不触碰用户全局 Git 配置。
    subprocess.run(
        ["git", "-C", str(wiki_dir), "config", "user.email", "wiki-agent-batch@localhost"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_dir), "config", "user.name", "Wiki Agent Batch"], check=True
    )
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
    """将可能嵌套的笔记复制成 compile_sources 可读取的扁平目录。"""
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
    reconcile: bool = False,
    commit_scope: str = "batch",
) -> dict[str, Any]:
    payload = load_manifest(manifest)
    root = root.expanduser().resolve()
    wiki_dir = wiki_dir.expanduser().resolve()
    if commit_scope not in {"source", "batch", "run"}:
        raise ValueError("commit-scope 必须是 source、batch 或 run")
    requested_batches = (
        split_sources(payload["sources"], batch_size) if batch_size else [payload["sources"]]
    )
    batches = (
        [[item] for item in payload["sources"]]
        if commit_scope == "source"
        else [payload["sources"]]
        if commit_scope == "run"
        else requested_batches
    )
    manifest_hash = _sha256(manifest.resolve())

    if resume:
        if not state_path.is_file():
            raise FileNotFoundError(f"找不到 resume state: {state_path}")
        state = _read_json(state_path)
        if reconcile:
            state = _reconcile_state(
                state,
                manifest=manifest,
                root=root,
                wiki_dir=wiki_dir,
                batch_size=batch_size,
                commit_scope=commit_scope,
                batches=batches,
                manifest_hash=manifest_hash,
            )
            _write_json_atomic(state_path, state)
        else:
            _validate_resume(state, manifest, root, wiki_dir, batches, manifest_hash)
        if not reconcile and state.get("batch_size") != batch_size:
            raise ValueError("resume 时不能修改 batch-size")
        if not reconcile and state.get("commit_scope", "batch") != commit_scope:
            raise ValueError("resume 时不能修改 commit-scope")
    else:
        state = _new_state(
            manifest, root, wiki_dir, batch_size, batches, manifest_hash, commit_scope
        )
        state["commit_scope"] = commit_scope
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

        async def checkpoint(source_name: str, status: str) -> None:
            source_id = next(
                (
                    item["id"]
                    for item in source_batch
                    if f"{item['id']}__{Path(item['path']).name}" == source_name
                ),
                None,
            )
            if source_id is not None:
                state["source_state"][source_id]["status"] = status
                state["source_state"][source_id]["updated_at"] = _now()
                _write_json_atomic(state_path, state)

        state["updated_at"] = _now()
        _write_json_atomic(state_path, state)
        try:
            run_dir = await compile_sources(
                batch_dir, wiki_dir=wiki_dir, source_checkpoint=checkpoint
            )
            run_record = _read_json(run_dir / "run.json")
            record["run_dir"] = str(run_dir)
            record["commit"] = run_record.get("commit")
            record["failed_source_ids"] = _failed_source_ids(run_dir, source_batch)
            record["finished_at"] = _now()
            if run_record.get("status") == "committed" and not record["failed_source_ids"]:
                record["status"] = "committed"
                for source_id in record["source_ids"]:
                    state["source_state"][source_id].update(
                        {"status": "committed", "completed_stage": "execute", "error": None}
                    )
            elif record["failed_source_ids"]:
                record["status"] = "failed"
                record["error"] = "source 失败: " + ", ".join(record["failed_source_ids"])
                for source_id in record["failed_source_ids"]:
                    state["source_state"][source_id].update(
                        {"status": "failed", "error": "source ingest failure"}
                    )
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
    parser.add_argument("--root", type=Path, help="笔记根目录（manifest.path 相对于它）")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--commit-scope",
        choices=("source", "batch", "run"),
        default="batch",
        help="Git 提交边界：每个 source、每个 batch，或整个 run",
    )
    parser.add_argument("--state", type=Path, help="状态文件；默认写入 workspace/manifests/")
    parser.add_argument("--work-dir", type=Path, help="批次临时 source 目录")
    parser.add_argument(
        "--resume", action="store_true", help="读取 state，跳过已经 committed 的批次"
    )
    parser.add_argument(
        "--status", action="store_true", help="只读显示 state 中的 source/batch 状态"
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="允许 manifest/source 变化，按 source id/hash 重建恢复计划",
    )
    parser.add_argument("--max-batches", type=int, help="最多执行几个批次，便于先做小规模试跑")
    parser.add_argument(
        "--init-git", action="store_true", help="wiki-dir 没有 Git 仓库时初始化临时仓库"
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(project_root=Path.cwd())
    manifest_workspace = cfg.paths.resolved_workspace_dir() / "manifests"
    state = args.state
    if args.status:
        if state is None:
            if args.wiki_dir is None:
                raise SystemExit("--status 需要 --state，或同时提供 --wiki-dir 以推导状态文件")
            state = manifest_workspace / f"{args.wiki_dir.expanduser().resolve().name}.json"
        if not state.is_file():
            raise SystemExit(f"找不到状态文件: {state}")
        payload = _read_json(state)
        source_counts: dict[str, int] = {}
        for item in payload.get("source_state", {}).values():
            status = str(item.get("status", "unknown"))
            source_counts[status] = source_counts.get(status, 0) + 1
        batch_counts: dict[str, int] = {}
        for item in payload.get("batches", []):
            status = str(item.get("status", "unknown"))
            batch_counts[status] = batch_counts.get(status, 0) + 1
        print(
            json.dumps(
                {"state": str(state), "sources": source_counts, "batches": batch_counts},
                ensure_ascii=False,
            )
        )
        return 0
    missing = [
        name
        for name in ("--root", "--manifest", "--wiki-dir")
        if getattr(args, name[2:].replace("-", "_"), None) is None
    ]
    if missing:
        raise SystemExit(f"编译运行缺少参数: {', '.join(missing)}")
    wiki_dir = args.wiki_dir.expanduser().resolve()
    if args.init_git:
        _init_git(wiki_dir)
    elif not (wiki_dir / ".git").exists():
        raise SystemExit("wiki-dir 不是 Git 仓库；临时评测请加 --init-git")
    state = args.state or (manifest_workspace / f"{wiki_dir.name}.json")
    work_dir = args.work_dir or (cfg.paths.resolved_workspace_dir() / "staging" / wiki_dir.name)
    result = await run_batches(
        root=args.root,
        manifest=args.manifest,
        wiki_dir=wiki_dir,
        batch_size=args.batch_size,
        state_path=state,
        work_dir=work_dir,
        resume=args.resume,
        max_batches=args.max_batches,
        reconcile=args.reconcile,
        commit_scope=args.commit_scope,
    )
    counts: dict[str, int] = {}
    for batch in result["batches"]:
        counts[batch["status"]] = counts.get(batch["status"], 0) + 1
    print(json.dumps({"state": str(state), "batches": counts}, ensure_ascii=False))
    return 1 if counts.get("failed") or counts.get("interrupted") else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parser().parse_args())))
