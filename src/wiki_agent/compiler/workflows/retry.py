"""source 失败队列的实际重试服务。

脚本和交互命令都调用这里，避免两套重试行为漂移。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from wiki_agent.compiler.wiki.quality import format_scan_report, scan_wiki
from wiki_agent.compiler.workflows.failures import SourceFailureConsumer
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.compiler.workflows.refine import build_index_excluding_self
from wiki_agent.config import CompileConfig, RetryConfig
from wiki_agent.ingestion.data_loader import DataLoader
from wiki_agent.log import emit_event
from wiki_agent.queue import QueueStore
from wiki_agent.versioning import WikiGitManager


async def retry_source_failures(
    queue: QueueStore,
    *,
    llm,
    vlm,
    wiki_dir: str | Path,
    compile_config: CompileConfig | None = None,
    retry_config: RetryConfig | None = None,
    item_id: str | None = None,
) -> dict:
    """按队列策略重试 source 失败项。"""
    wiki = Path(wiki_dir).resolve()
    items = [item for item in queue.list() if item.get("type") == "ingest_failure"]
    if item_id is not None:
        items = [item for item in items if item.get("id") == item_id]
    if not items:
        return {
            "results": [],
            "committed": False,
            "rolled_back": False,
            "scan_errors": 0,
            "message": "没有匹配的 source 失败项。",
        }

    run_id = f"retry_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    manager = WikiGitManager(wiki, run_root=wiki / ".logs" / "runs")
    git_run = manager.begin(run_id, mode="retry")
    config = compile_config or CompileConfig()

    async def process(item: dict) -> None:
        source_path = Path(item.get("source_path") or item.get("file", ""))
        if not source_path.is_absolute():
            source_path = (Path.cwd() / source_path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"source 不存在: {source_path}")

        mode = item.get("mode", "compile")
        pipeline = CompilePipeline(
            llm=llm,
            vlm=vlm,
            wiki_dir=wiki,
            mode=mode,
            compile_config=config,
            index_reader=(build_index_excluding_self(wiki) if mode == "refine" else None),
        )
        summary = DataLoader().load([source_path])
        if not summary.files:
            raise ValueError(f"无法加载 source: {source_path}")
        await pipeline.ingest_one(summary.files[0])

    retry = retry_config or RetryConfig()
    consumer = SourceFailureConsumer(
        queue,
        process,
        remove_on_success=False,
        retry_config=retry,
    )
    results = []
    try:
        for item in items:
            results.append(await consumer.consume(item))

        failed = [item for item in results if item["status"] == "failed"]
        if failed:
            _restore_succeeded_queue_items(queue, results)
            manager.abort(git_run, reason=f"source retry failures: {len(failed)}")
            return {"results": results, "committed": False, "rolled_back": True, "scan_errors": 0}

        issues = scan_wiki(wiki)
        scan_errors = sum(issue.level == "error" for issue in issues)
        report = git_run.run_dir / "scan_report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(format_scan_report(issues), encoding="utf-8")
        if scan_errors:
            _restore_succeeded_queue_items(queue, results)
            manager.abort(git_run, reason=f"retry scan errors: {scan_errors}")
            return {
                "results": results,
                "committed": False,
                "rolled_back": True,
                "scan_errors": scan_errors,
            }

        manager.commit(
            git_run,
            message=f"wiki: retry failures {git_run.run_id}",
            scan_report=report,
            metadata={"results": results, "scan_errors": scan_errors},
        )
        for result in results:
            if result["status"] == "succeeded":
                queue.remove(result["id"])
        emit_event("source_retry_committed", run_id=git_run.run_id, results=results)
        return {
            "results": results,
            "committed": True,
            "rolled_back": False,
            "scan_errors": scan_errors,
        }
    except asyncio.CancelledError:
        _restore_succeeded_queue_items(queue, results)
        manager.abort(git_run, reason="source retry cancelled")
        raise
    except Exception:
        _restore_succeeded_queue_items(queue, results)
        manager.abort(git_run, reason="source retry exception")
        raise


def _restore_succeeded_queue_items(queue: QueueStore, results: list[dict]) -> None:
    for result in results:
        if result.get("status") == "succeeded":
            queue.update(result["id"], status="pending")
