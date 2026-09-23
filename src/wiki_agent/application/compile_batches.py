"""Manifest 分批评测编排——materialize 嵌套 source 后逐批走快照 sync。

写 wiki 只有队列一条路，本模块不造第二执行通道：每批只是把 manifest
指向的嵌套文件复制成一个扁平 staging 目录，然后对它执行一次 sync
（submit_sync + 自泵到队列空）。

因此这里没有进度账本——进度 = sync 完成账（state.json）+ jobs 队列：
中断后重跑同一命令，已成功的内容按账本不再入队，未跑完的重新拍进快照。
本模块不落任何编排状态，也没有与之对应的参数。

示例::

    uv run python -m wiki_agent.application.compile_batches \
        --root /path/to/notebook --manifest /path/to/source_manifest.json \
        --wiki-dir /tmp/wiki-agent-batched --batch-size 20

workspace（评测沙箱的 jobs/账本/events 所在）默认取配置的 workspace，
评测请显式传 --workspace 指向隔离目录，避免污染正式完成账。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from wiki_agent.config import (
    RootConfig,
    load_config,
    source_records_dir_for,
    sync_state_path_for,
)
from wiki_agent.exec_lock import acquire_execution_lock, release_execution_lock

# 注入点：一批 = 一次快照 sync + 泵到空（单测替换，不碰 LLM）
SyncExecute = Callable[[Path], Awaitable[int]]


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


def materialize_batch(
    root: Path,
    sources: list[dict[str, Any]],
    batch_dir: Path,
) -> None:
    """将可能嵌套的笔记复制成 sync 可扫描的扁平目录。

    先清空再复制——staging 路径即账本键（`id__文件名`），重跑内容
    逐字节一致时 digest 不变、按账本不再入队。
    """
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    batch_dir.mkdir(parents=True)
    for item in sources:
        source = (root / item["path"]).resolve()
        if not source.is_file() or not source.is_relative_to(root):
            raise FileNotFoundError(f"source 不存在或越过 root: {item['path']}")
        shutil.copy2(source, batch_dir / f"{item['id']}__{source.name}")


async def run_manifest(
    *,
    root: Path,
    manifest: Path,
    wiki_dir: Path,
    workspace: Path,
    work_dir: Path,
    batch_size: int,
    max_batches: int | None = None,
    execute: SyncExecute | None = None,
) -> dict[str, Any]:
    """逐批 materialize → sync；返回统计，不落任何编排状态。"""
    payload = load_manifest(manifest)
    root = root.expanduser().resolve()
    batches = split_sources(payload["sources"], batch_size)
    executor = execute or _make_sync_executor(
        workspace=workspace.expanduser().resolve(),
        wiki_dir=wiki_dir.expanduser().resolve(),
    )
    run_dir = work_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    enqueued = 0
    executed = 0
    for index, batch in enumerate(batches):
        if max_batches is not None and executed >= max_batches:
            break
        batch_dir = run_dir / f"batch-{index:03d}"
        materialize_batch(root, batch, batch_dir)
        enqueued += await executor(batch_dir)
        executed += 1
    return {"batches_run": executed, "batches_total": len(batches), "enqueued": enqueued}


def _make_sync_executor(*, workspace: Path, wiki_dir: Path) -> SyncExecute:
    """默认执行器：装配一个隔离于 AppRuntime 的小型 sync 执行现场。

    评测沙箱自带 workspace 的 jobs/账本与目标 wiki；LLM 客户端按项目
    配置构造。执行锁让同一沙箱目录同时只有一个跑批进程。
    """

    async def execute(batch_dir: Path) -> int:
        from wiki_agent.compiler.workflows.ingest import CompilePipeline
        from wiki_agent.jobs.service import JobService
        from wiki_agent.jobs.worker import JobWorker
        from wiki_agent.log import setup_event_log
        from wiki_agent.sync.job_consumer import SyncConsumer
        from wiki_agent.sync.state import SyncState
        from wiki_agent.versioning import WikiGitManager

        cfg: RootConfig = load_config(project_root=Path.cwd())
        acquire_execution_lock(workspace)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            setup_event_log(workspace / "logs" / "compile-batches-events.jsonl")
            source_records_dir = source_records_dir_for(workspace)
            state = SyncState(sync_state_path_for(workspace))
            service = JobService(
                workspace,
                wiki_dir=wiki_dir,
                sync_state=state,
                source_records_dir=source_records_dir,
            )
            from wiki_agent.llm.factory import create_llm, create_vlm

            pipeline = CompilePipeline(
                llm=create_llm(cfg.llm, cfg.retry),
                vlm=create_vlm(cfg.vlm, cfg.retry),
                wiki_dir=wiki_dir,
                source_records_dir=source_records_dir,
                compile_config=cfg.compile,
            )
            consumer = SyncConsumer(
                pipeline,
                state,
                wiki_dir=wiki_dir,
                source_records_dir=source_records_dir,
                snapshots=service.snapshots,
                git=WikiGitManager(wiki_dir),
            )
            worker = JobWorker(service)
            worker.register("compile", consumer.handle_job)
            worker.register("delete", consumer.handle_job)
            jobs = service.submit_sync(batch_dir)
            while service.store.count_in_flight() > 0:
                if await worker.run_once() is None:
                    break
            return len(jobs)
        finally:
            release_execution_lock(workspace)

    return execute


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="笔记根目录（manifest.path 相对于它）")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="评测沙箱 workspace（jobs/账本/events）；默认取配置的 workspace",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--work-dir", type=Path, help="staging 目录；默认 workspace/staging/<wiki名>")
    parser.add_argument("--max-batches", type=int, help="最多跑几批，便于小规模试跑")
    return parser


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(project_root=Path.cwd())
    if args.root is None:
        raise SystemExit("--root 必填（manifest.path 相对它的根目录）")
    wiki_dir = args.wiki_dir.expanduser().resolve()
    workspace = (
        args.workspace.expanduser().resolve()
        if args.workspace is not None
        else cfg.paths.resolved_workspace_dir()
    )
    work_dir = args.work_dir or (workspace / "staging" / wiki_dir.name)
    result = await run_manifest(
        root=args.root,
        manifest=args.manifest,
        wiki_dir=wiki_dir,
        workspace=workspace,
        work_dir=work_dir,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parser().parse_args())))
