"""source 失败队列的实际重试服务。

脚本和交互命令都调用这里，避免两套重试行为漂移。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from wiki_agent.compiler.workflows.failures import SourceFailureConsumer
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.compiler.workflows.refine import build_index_excluding_self
from wiki_agent.config import CompileConfig, RetryConfig
from wiki_agent.documents.loader import DataLoader
from wiki_agent.issues import IssueKind, IssueRecord, IssueStatus, IssueStore
from wiki_agent.log import emit_event
from wiki_agent.versioning import WikiGitManager
from wiki_agent.wiki.quality import format_scan_report, scan_wiki


class SourceUnavailableError(FileNotFoundError):
    """失败记录所指向的输入已经无法恢复。"""


def resolve_retry_source(issue: IssueRecord, wiki_dir: str | Path) -> Path:
    """从问题记录解析当前可用的重试输入。

    refine 的输入是 Wiki 页面。旧评测记录可能保留了已消失的
    ``/tmp`` 绝对路径，此时允许用公开页面名在当前 Wiki 中唯一
    定位。compile 输入不能猜测，原始来源丢失时必须由用户重新提供。
    """
    wiki = Path(wiki_dir).resolve()
    raw_path = str(issue.context.get("source_path") or "").strip()
    source_kind = str(issue.resource.get("type") or "input_file")
    public_name = str(issue.resource.get("path") or "").strip()

    if raw_path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if candidate.is_file():
            if source_kind != "wiki_page":
                return candidate
            try:
                candidate.relative_to(wiki)
            except ValueError:
                pass
            else:
                return candidate

    if source_kind == "wiki_page" and public_name:
        relative = Path(public_name)
        direct = (wiki / relative).resolve()
        try:
            direct.relative_to(wiki)
        except ValueError:
            direct = wiki / "__invalid__"
        if direct.is_file():
            return direct
        matches = [
            path.resolve()
            for folder in ("concepts", "entities", "topics")
            for path in (wiki / folder).rglob(relative.name)
            if path.is_file()
        ]
        unique = list(dict.fromkeys(matches))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            raise SourceUnavailableError(
                f"Wiki 中存在多个同名页面，无法确定重试对象: {relative.name}"
            )
        raise SourceUnavailableError(f"Wiki 页面已不存在，无法重试: {relative.name}")

    label = public_name or raw_path or "未知来源"
    raise SourceUnavailableError(f"原始来源已不存在，请重新提供后再编译: {Path(label).name}")


async def retry_source_failures(
    store: IssueStore,
    *,
    llm,
    vlm,
    wiki_dir: str | Path,
    source_records_dir: str | Path | None = None,
    run_root: str | Path | None = None,
    compile_config: CompileConfig | None = None,
    retry_config: RetryConfig | None = None,
    issue_id: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    force: bool = False,
) -> dict:
    """按数据库中的重试策略处理 source 失败问题。"""
    wiki = Path(wiki_dir).resolve()
    workspace = store.workspace
    source_records_path = (
        Path(source_records_dir).resolve()
        if source_records_dir is not None
        else workspace / "provenance" / "sources"
    )
    runs = Path(run_root).resolve() if run_root is not None else workspace / "runs"
    issue_records = store.list(
        statuses={IssueStatus.OPEN, IssueStatus.BLOCKED, IssueStatus.PROCESSING},
        kinds={IssueKind.INGESTION_FAILURE},
        limit=1000,
    )
    if issue_id is not None:
        issue_records = [record for record in issue_records if record.id == issue_id]
    if not issue_records:
        return {
            "results": [],
            "committed": False,
            "rolled_back": False,
            "scan_errors": 0,
            "message": "没有匹配的 source 失败项。",
        }

    # Web 中的手动重试先做可恢复性检查，避免为已消失的来源
    # 建立 Git 事务和瞬时失败任务。自动消费仍由 consumer 记录失败。
    if force:
        for record in issue_records:
            resolve_retry_source(record, wiki)

    def notify(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    run_id = f"retry_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    notify("prepare")
    manager = WikiGitManager(wiki, run_root=runs)
    git_run = manager.begin(run_id, mode="retry")
    config = compile_config or CompileConfig()

    async def process(record: IssueRecord) -> None:
        source_path = resolve_retry_source(record, wiki)

        notify("load")
        mode = str(record.origin.get("mode") or "compile")
        pipeline = CompilePipeline(
            llm=llm,
            vlm=vlm,
            wiki_dir=wiki,
            source_records_dir=source_records_path,
            mode=mode,
            compile_config=config,
            index_reader=(build_index_excluding_self(wiki) if mode == "refine" else None),
            on_progress=notify,
        )
        summary = DataLoader().load([source_path])
        if not summary.files:
            raise ValueError(f"无法加载 source: {source_path}")
        await pipeline.ingest_one(summary.files[0])

    retry = retry_config or RetryConfig()
    consumer = SourceFailureConsumer(
        store,
        process,
        retry_config=retry,
    )
    results = []
    try:
        for record in issue_records:
            results.append(await consumer.consume(record, force=force))

        failed = [item for item in results if item["status"] == "failed"]
        if failed:
            notify("rollback")
            manager.abort(git_run, reason=f"source retry failures: {len(failed)}")
            errors = [str(item.get("error") or "") for item in failed if item.get("error")]
            diagnostics = failed[0].get("diagnostics", {})
            return {
                "results": results,
                "committed": False,
                "rolled_back": True,
                "scan_errors": 0,
                "message": errors[0] if errors else "source 重试失败，Wiki 已回撤",
                "diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
            }

        notify("scan")
        issues = scan_wiki(wiki)
        scan_errors = sum(issue.level == "error" for issue in issues)
        scan_report = git_run.run_dir / "scan_report.md"
        scan_report.parent.mkdir(parents=True, exist_ok=True)
        scan_report.write_text(format_scan_report(issues), encoding="utf-8")
        if scan_errors:
            notify("rollback")
            manager.abort(git_run, reason=f"retry scan errors: {scan_errors}")
            return {
                "results": results,
                "committed": False,
                "rolled_back": True,
                "scan_errors": scan_errors,
                "message": f"重试产物质量扫描发现 {scan_errors} 个错误，Wiki 已回撤",
                "diagnostics": {
                    "detail": f"重试产物质量扫描发现 {scan_errors} 个错误",
                    "stage": "scan",
                    "error_code": "wiki_scan_failed",
                    "error_class": "content",
                    "scan_errors": scan_errors,
                },
            }

        notify("commit")
        manager.commit(
            git_run,
            message=f"wiki: retry failures {git_run.run_id}",
            scan_report=scan_report,
            metadata={"results": results, "scan_errors": scan_errors},
        )
        emit_event("source_retry_committed", run_id=git_run.run_id, results=results)
        return {
            "results": results,
            "committed": True,
            "rolled_back": False,
            "scan_errors": scan_errors,
        }
    except asyncio.CancelledError:
        manager.abort(git_run, reason="source retry cancelled")
        raise
    except Exception:
        manager.abort(git_run, reason="source retry exception")
        raise
