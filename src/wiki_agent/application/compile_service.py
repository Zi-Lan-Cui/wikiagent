"""Application service for compiling source documents into the Wiki."""

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from wiki_agent.compiler.workflows.failures import SourceFailureHandler
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.config import load_config
from wiki_agent.documents.loader import DataLoader
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.issues.producers import report_quality_findings
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.log import begin_trace, configure_logging, emit_event, get_logger, setup_event_log
from wiki_agent.versioning import WikiGitManager
from wiki_agent.wiki.quality import format_scan_report, scan_source, scan_wiki

# wiki_agent logger（已接入 run.log）——异常回溯用它的 exc_info，
# 不手工拼 traceback（logging 内建能力）
_boundary_logger = get_logger("COMPILE_BOUNDARY")

# 运行目录在 main() 内初始化——import 时定格会导致
# "import 模块、很久后再调 main"时容器名与实际运行时间错位


_LEVEL_MAP = {
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "DEBUG": logging.DEBUG,
}


def log(msg: str, level: str = "INFO") -> None:
    """终端 + 文件双通道——文件写入统一走 wiki_agent logger。

    文件写入只经过 logging，避免重复记录。

    Args:
        msg: 消息文本。
        level: 级别名（INFO/WARN/ERROR/DEBUG）。
    """
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] [{level}] {msg}")
    _boundary_logger.log(_LEVEL_MAP.get(level, logging.INFO), msg)


async def compile_sources(
    source_dir: str | Path | None = None,
    *,
    project_root: Path | None = None,
    wiki_dir: Path | None = None,
    progress: Callable[..., Awaitable[None]] | None = None,
    source_checkpoint: Callable[[str, str], Awaitable[None] | None] | None = None,
) -> Path:
    """编译主流程——加载 → 逐文件流水线 → 汇总 → 质量扫描。

    这是可复用的函数入口；脚本入口和 CLI `/compile` 都调用它。

    Args:
        source_dir: 原始资料文件夹；省略时使用 ``WIKI_MATERIALS_DIR``。
    """
    project_root = (project_root or Path.cwd()).resolve()
    cfg = load_config(project_root=project_root)
    wiki_dir = (
        Path(wiki_dir).resolve()
        if wiki_dir is not None
        else cfg.paths.resolved_wiki_dir().resolve()
    )
    workspace_dir = cfg.paths.resolved_workspace_dir().resolve()
    source_records_dir = cfg.paths.resolved_source_records_dir().resolve()
    runs_dir = cfg.paths.resolved_runs_dir().resolve()
    source_path = (
        Path(source_dir).expanduser().resolve()
        if source_dir is not None
        else cfg.paths.resolved_materials_dir().resolve()
    )
    if not source_path.is_dir():
        raise FileNotFoundError(f"源目录不存在: {source_path}")

    async def report(
        stage: str,
        *,
        current: int | None = None,
        total: int | None = None,
        message: str = "",
        level: str = "info",
        data: dict | None = None,
    ) -> None:
        if progress is not None:
            await progress(
                stage,
                current=current,
                total=total,
                message=message,
                level=level,
                data=data or {},
            )

    # 运行目录（真正运行时才确定时间戳——不是 import 时）
    _run_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    RUN_DIR = runs_dir / f"compile_{_run_ts}"
    ARTIFACTS_DIR = RUN_DIR / "artifacts"
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _run_log = RUN_DIR / "run.log"

    # Git dirty 检查必须发生在本次 run.log/events.jsonl 创建之前；否则
    # 编译器自己的审计文件会被误判为用户修改，导致临时 Wiki 或未配置
    # .gitignore 的 Wiki 无法启动。begin 只创建 checkpoint 和锁，不依赖
    # LLM，因此先建立事务边界，再接入运行日志。
    git_manager = WikiGitManager(wiki_dir, run_root=runs_dir)
    git_run = None
    try:
        git_run = git_manager.begin(_run_ts, mode="compile")
    except Exception:
        raise

    # wiki_agent logger 接入本运行日志——否则 LLM 错误/页面生成失败
    # 只走 stderr（lastResort），运行结束后证据消失（审计 E1）
    configure_logging(file_path=str(_run_log))
    # 事件流（机器消费: ingest_failure/plan_noop/file_skipped...）
    setup_event_log(RUN_DIR / "events.jsonl")

    # 本次运行一个 trace——事件流里的 ingest_file/llm_attempt
    # 全部挂在这个 trace_id 下，跨进程对比两个 run 的差异时可分组
    begin_trace(trace_id=f"compile_{_run_ts}")

    log(f"=== 开始编译: {source_path} ===")
    log(f"日志文件: {_run_log}")
    log(f"Wiki 输出: {wiki_dir}")
    started = time.monotonic()
    await report("init", message="正在初始化编译器")

    # 统计
    files_loaded = 0
    files_converted = 0
    files_chunked = 0
    files_extracted = 0
    pages_created = 0
    skip_errors: list[IngestError] = []
    skipped_files: list[dict[str, str]] = []

    # 0. 初始化
    log("0. 初始化 LLM + VLM")
    try:
        llm = create_llm(cfg.llm, cfg.retry)
        vlm = create_vlm(cfg.vlm, cfg.retry)
    except Exception as e:
        log(f"LLM 初始化失败: {e}", "ERROR")
        if git_run is not None:
            git_manager.abort(git_run, reason=f"initialization failed: {e}")
        raise RuntimeError(f"LLM 初始化失败: {e}") from e
    log(f"   LLM: {llm.model_id}  |  VLM: {vlm.model_id}")

    issue_service = IssueService(IssueStore(workspace_dir))
    failure_handler = SourceFailureHandler(issue_service, mode="compile")

    # 1. DataLoader
    log("1. DataLoader: 扫描文件")
    loader = DataLoader()
    summary = loader.load_dir(source_path)
    files_loaded = summary.loaded_count
    await report(
        "load",
        current=files_loaded,
        total=summary.total_found,
        message=f"已加载 {files_loaded}/{summary.total_found} 个 source",
    )
    log(f"   发现: {summary.total_found}  加载: {files_loaded}  跳过: {summary.skipped_count}")
    for entry in summary.skipped[:10]:
        log(f"   ✗ 跳过 {entry.get('name', entry)} — {entry.get('reason', '')}", "WARN")
    skipped_files.extend(
        {
            "source": str(entry.get("name", "")),
            "kind": "load_skip",
            "reason": str(entry.get("reason", "未知原因")),
        }
        for entry in summary.skipped
    )
    if not summary.files:
        log("无文件可处理", "ERROR")
        raise ValueError(f"source 目录中没有可处理的文件: {source_dir}")

    # 2-5. 逐文件: 单文件流水线（compile/sync 共用入口）
    pipeline = CompilePipeline(
        llm=llm,
        vlm=vlm,
        wiki_dir=wiki_dir,
        source_records_dir=source_records_dir,
        compile_config=cfg.compile,
    )

    def _record_failure(stage: IngestStage, source: str, source_path: Path, exc: Exception) -> None:
        """边界统一收集失败——异常对象即汇总记录。

        - log()/logger: 给人看（摘要 + exc_info 回溯进 run.log）
        - ingest_failure 事件: 给机器看（全量 error/cause/raw——
          机器通道不截断，截断是给人看的习惯；事件流是唯一机器事实源）
        - 统一队列: 待处理事项的人机接口（/queue 列出、处理后移除）
        - skip_errors: 内存列表，只服务汇总打印（人看），不落第二份文件

        Args:
            stage: 失败阶段。
            source: 源文件名。
            exc: 原始异常。
        """
        _boundary_logger.error(
            "编译失败 [%s] %s: %s",
            stage.value,
            source,
            str(exc)[:200],
            exc_info=exc,
        )
        err = failure_handler.handle(exc, source=source, source_path=source_path)
        skip_errors.append(err)
        skipped_files.append(
            {
                "source": source,
                "kind": "failure",
                "reason": str(err),
                "stage": err.stage.value,
                "error_code": err.error_code,
                "error_class": err.error_class or "unknown",
                "retry_policy": err.retry_policy or "manual",
            }
        )

    def _save_artifact(artifact_dir: Path, name: str, content: str) -> None:
        """保存中间产物到 run 容器。

        Args:
            artifact_dir: 产物目录。
            name: 文件名。
            content: 内容。
        """
        (artifact_dir / name).write_text(content, encoding="utf-8")

    for idx, raw_file in enumerate(summary.files):
        file_no = f"[{idx + 1}/{files_loaded}]"
        log(f"{file_no} {raw_file.name}")
        await report(
            "ingest",
            current=idx,
            total=files_loaded,
            message=f"正在处理 {raw_file.name}",
            data={"source": raw_file.name},
        )

        # 本文件的产物目录——原始材料按源文件归拢，审计/调试可回溯
        artifact_dir = ARTIFACTS_DIR / raw_file.name
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if source_checkpoint is not None:
            result = source_checkpoint(raw_file.name, "running")
            if asyncio.iscoroutine(result):
                await result

        try:
            outcome = await pipeline.ingest_one(raw_file)
        except asyncio.CancelledError as exc:
            if source_checkpoint is not None:
                result = source_checkpoint(raw_file.name, "interrupted")
                if asyncio.iscoroutine(result):
                    await result
            git_manager.abort(git_run, reason=f"compile cancelled: {exc}")
            raise
        except IngestError as e:
            if source_checkpoint is not None:
                result = source_checkpoint(raw_file.name, "failed")
                if asyncio.iscoroutine(result):
                    await result
            _record_failure(e.stage, raw_file.name, raw_file.path, e)
            await report(
                "ingest",
                current=idx + 1,
                total=files_loaded,
                message=f"处理失败：{raw_file.name}",
                level="error",
                data={"source": raw_file.name, "stage": e.stage.value},
            )
            continue

        # 单 source 局部闸门：先检查本次 source 的档案页和生成页；
        # 失败归属当前 source，交给统一异常队列。scan_wiki 只在批次末尾
        # 做全库关系/索引/重复页收尾，不让原始 source 文本误触发全库回滚。
        local_issues = scan_source(
            wiki_dir,
            source_name=raw_file.name,
            source_records_dir=source_records_dir,
            generated_paths=outcome.pages_written,
        )
        local_errors = [issue for issue in local_issues if issue.level == "error"]
        if local_errors:
            if source_checkpoint is not None:
                result = source_checkpoint(raw_file.name, "failed")
                if asyncio.iscoroutine(result):
                    await result
            local_reason = "; ".join(str(issue) for issue in local_errors[:3])
            _record_failure(
                IngestStage.EXECUTE,
                raw_file.name,
                raw_file.path,
                IngestError(
                    IngestStage.EXECUTE,
                    f"单 source 质量检查失败: {local_reason}",
                    source=raw_file.name,
                    error_code="source_quality_error",
                ),
            )
            await report(
                "ingest",
                current=idx + 1,
                total=files_loaded,
                message=f"单 source 质量检查失败：{raw_file.name}",
                level="error",
                data={"source": raw_file.name, "stage": IngestStage.EXECUTE.value},
            )
            continue

        files_converted += 1
        if outcome.extract:
            files_extracted += 1
            _save_artifact(artifact_dir, "extract.json", outcome.extract.document_summary)
        if outcome.search:
            _save_artifact(
                artifact_dir,
                "search.json",
                json.dumps(
                    {
                        "source": raw_file.name,
                        "rel_paths": outcome.search.rel_paths,
                        "raw": outcome.search.raw,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        if outcome.analysis:
            _save_artifact(
                artifact_dir,
                "analyze.json",
                json.dumps(
                    {
                        "source": raw_file.name,
                        "analysis_text": outcome.analysis.analysis_text,
                        "raw": outcome.analysis.raw_analysis,
                        "entities": outcome.analysis.entities,
                        "concepts": outcome.analysis.concepts,
                        "relationships": [
                            {
                                "from_page": rel.from_page,
                                "to_page": rel.to_page,
                                "relation": rel.relation,
                                "detail": rel.detail,
                            }
                            for rel in outcome.analysis.relationships
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        if outcome.plan:
            _save_artifact(
                artifact_dir,
                "plan.json",
                json.dumps(
                    {
                        "source": raw_file.name,
                        "page_targets": [
                            {
                                "wiki_path": pt.wiki_path,
                                "title": pt.title,
                                "disposition": pt.disposition.value,
                                "page_type": pt.page_type,
                                "reason": pt.reason,
                            }
                            for pt in outcome.plan.page_targets
                        ],
                        "raw": outcome.plan.raw,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            n = len(outcome.plan.page_targets)
            log(f"  {n} 个受影响页面")
            for pt in outcome.plan.page_targets:
                log(f"    [{pt.disposition.value}] {pt.wiki_path} — {pt.title}")
                log(f"      reason: {pt.reason}")
            if n == 0:
                emit_event("plan_noop", file=raw_file.name, reason="page_targets 为空")
                skipped_files.append(
                    {
                        "source": raw_file.name,
                        "kind": "plan_noop",
                        "reason": "plan.page_targets 为空",
                    }
                )
                continue

        # 页面副本存档
        pages_log_dir = artifact_dir / "pages"
        pages_log_dir.mkdir(exist_ok=True)
        for wiki_path in outcome.pages_written:
            full = wiki_dir / wiki_path
            page_content = full.read_text(encoding="utf-8")
            pages_created += 1
            log(f"    ✓ {wiki_path} ({len(page_content)} chars)")
            safe_name = wiki_path.replace("/", "_")
            (pages_log_dir / safe_name).write_text(
                f"# generated at: {datetime.now().isoformat()}\n\n{page_content}",
                encoding="utf-8",
            )
        await report(
            "ingest",
            current=idx + 1,
            total=files_loaded,
            message=f"已完成 {raw_file.name}",
            data={"source": raw_file.name, "pages_written": len(outcome.pages_written)},
        )
        if source_checkpoint is not None:
            result = source_checkpoint(raw_file.name, "completed")
            if asyncio.iscoroutine(result):
                await result

    # 6. 汇总
    elapsed = time.monotonic() - started
    log(f"\n=== 编译完成: {elapsed:.1f}s ===")
    log(f"   文件加载: {files_loaded}")
    log(f"   转换完成: {files_converted}")
    log(f"   Chunk完成: {files_chunked}")
    log(f"   Extract完成: {files_extracted}")
    log(f"   页面创建: {pages_created}")
    log(f"   跳过文件: {len(skip_errors)}")
    if skip_errors:
        # 按阶段分组输出失败清单（人看）
        log("失败清单（按阶段）:", "ERROR")
        from collections import defaultdict

        by_stage: dict[str, list[IngestError]] = defaultdict(list)
        for err in skip_errors:
            by_stage[err.stage.value].append(err)
        for stage, items in by_stage.items():
            log(f"  [{stage}] {len(items)} 个文件:")
            for item in items:
                log(f"    - {item.source}: {item}", "ERROR")
            # 机器侧失败数据已在 events.jsonl（ingest_failure 全量
        # error/cause/raw）——不落第二份清单，避免双真相源漂移。
        # 未来"重试失败文件"工具直接过滤事件流。
        # 汇总建议
        retry_count = sum(1 for s in skip_errors if s.cause is not None and "未分类" not in str(s))
        log(f"  建议: 其中 {retry_count} 个失败可检查日志后重试该文件，未分类失败请排查代码")

    # 7. Wiki 质量扫描（后处理兜底）
    log("\n=== Wiki 质量扫描 ===")
    await report("scan", current=0, total=1, message="正在扫描 Wiki 质量")
    issues = scan_wiki(wiki_dir)
    report_quality_findings(
        issue_service,
        issues,
        origin={"mode": "compile", "run_id": RUN_DIR.name},
    )
    error_issues = [i for i in issues if i.level == "error"]
    warn_issues = [i for i in issues if i.level == "warning"]
    log(f"   扫描结果: {len(error_issues)} 错误, {len(warn_issues)} 警告")
    for i in issues:
        level = "ERROR" if i.level == "error" else "WARN"
        log(f"   {str(i)}", level)
    # 扫描报告进 run 目录（人看结论）
    (RUN_DIR / "scan_report.md").write_text(format_scan_report(issues), encoding="utf-8")

    # 人可读的本次编译差异：Git 的 patch/changed_files 仍保留为机器和审计
    # 用，这份报告额外解释“改了什么”和“哪些 source 没有产出页面”。
    change_summary = git_manager.change_summary(git_run)
    diff_lines = [
        f"# Compile diff: `{git_run.run_id}`",
        "",
        f"- 基线 commit: `{git_run.before_commit}`",
        f"- Wiki: `{wiki_dir}`",
        "",
    ]
    for title, key in (
        ("新增页面/文件", "added"),
        ("修改页面/文件", "modified"),
        ("删除页面/文件", "deleted"),
        ("重命名页面/文件", "renamed"),
    ):
        paths = change_summary[key]
        diff_lines.append(f"## {title}（{len(paths)}）")
        diff_lines.extend(f"- `{path}`" for path in paths)
        if not paths:
            diff_lines.append("- 无")
        diff_lines.append("")
    diff_lines.append(f"## 跳过的 source（{len(skipped_files)}）")
    if skipped_files:
        for item in skipped_files:
            detail = item["reason"].replace("\n", " ")
            extra = f"，stage={item['stage']}" if item.get("stage") else ""
            diff_lines.append(f"- `{item['source']}`（{item['kind']}{extra}）：{detail}")
    else:
        diff_lines.append("- 无")
    diff_lines.append("")
    (RUN_DIR / "compile_diff.md").write_text("\n".join(diff_lines), encoding="utf-8")
    log(f"编译 diff 报告: {RUN_DIR / 'compile_diff.md'}")
    if error_issues:
        git_manager.abort(git_run, reason=f"scan errors: {len(error_issues)}")
        log("Git: 已恢复到编译前版本", "ERROR")
    else:
        await report("git", current=0, total=1, message="正在提交 Wiki 变更")
        committed = git_manager.commit(
            git_run,
            message=f"wiki: compile {git_run.run_id}",
            scan_report=RUN_DIR / "scan_report.md",
            diff_report=RUN_DIR / "compile_diff.md",
            metadata={
                "files_loaded": files_loaded,
                "pages_created": pages_created,
                "skipped_files": skipped_files,
                "scan_errors": len(error_issues),
                "scan_warnings": len(warn_issues),
            },
        )
        log(f"Git: 已提交 {committed.commit}")
    await report("done", current=1, total=1, message="编译完成")

    # 输出 wiki 目录树
    log("\nwiki/ 目录结构:")
    for root, dirs, files in os.walk(wiki_dir):
        lvl = root.replace(str(wiki_dir), "").count(os.sep)
        ind = "  " * lvl
        log(f"{ind}{os.path.basename(root) or 'wiki'}/")
        for f in sorted(files)[:20]:
            log(f"{ind}  {f}")
        if len(files) > 20:
            log(f"{ind}  ... 共 {len(files)} 个文件")

    return RUN_DIR
