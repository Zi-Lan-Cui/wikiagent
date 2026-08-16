"""文件夹 → Wiki 编译脚本。

用法:
    cd /home/zilan/桌面/wiki_agent
    VIRTUAL_ENV= .venv/bin/python scripts/compile_folder.py /media/zilan/.../Python

数据流:
    folder/ → DataLoader → Converter (+VLM caption) → Chunker → Compiler(Extractor+Integrator) → wiki/
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 项目 src 加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.ingestion.data_loader import DataLoader
from wiki_agent.compiler.pipeline import CompilePipeline
from wiki_agent.compiler.quality import format_scan_report, scan_wiki
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.log import begin_trace, configure_logging, emit_event, get_logger, setup_event_log

# wiki_agent logger（已接入 run.log）——异常回溯用它的 exc_info，
# 不手工拼 traceback（logging 内建能力）
_boundary_logger = get_logger("COMPILE_BOUNDARY")

# ── 配置 ────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = PROJECT_ROOT / "wiki"
CHUNK_SIZE = 8_000
EXTRACT_CONCURRENCY = 3

# 运行目录在 main() 内初始化——import 时定格会导致
# "import 模块、很久后再调 main"时容器名与实际运行时间错位


_LEVEL_MAP = {
    "INFO": logging.INFO, "WARN": logging.WARNING,
    "ERROR": logging.ERROR, "DEBUG": logging.DEBUG,
}


def log(msg: str, level: str = "INFO") -> None:
    """终端 + 文件双通道——文件写入统一走 wiki_agent logger。

    （历史版本自己 append run.log——与 FileHandler 双写同一文件，
    两套机制并存。现在文件侧只有 logging 一条路。）
    """
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] [{level}] {msg}")
    _boundary_logger.log(_LEVEL_MAP.get(level, logging.INFO), msg)


# ════════════════════════════════════════════════════════════
#  主流程
# ════════════════════════════════════════════════════════════

async def main(source_dir: str):
    source_path = Path(source_dir).resolve()
    if not source_path.is_dir():
        log(f"源目录不存在: {source_dir}", "ERROR")
        sys.exit(1)

    # ── 运行目录（真正运行时才确定时间戳——不是 import 时）──
    _run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    RUN_DIR = WIKI_DIR / ".logs" / "runs" / f"compile_{_run_ts}"
    ARTIFACTS_DIR = RUN_DIR / "artifacts"
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _run_log = RUN_DIR / "run.log"

    # wiki_agent logger 接入本运行日志——否则 LLM 错误/页面生成失败
    # 只走 stderr（lastResort），运行结束后证据消失（审计 E1）
    configure_logging(file_path=str(_run_log))
    # 事件流（机器消费: compile_failure/plan_noop/file_skipped...）
    setup_event_log(RUN_DIR / "events.jsonl")

    # 本次运行一个 trace——事件流里的 ingest_file/llm_attempt
    # 全部挂在这个 trace_id 下，跨进程对比两个 run 的差异时可分组
    begin_trace(trace_id=f"compile_{_run_ts}")

    log(f"=== 开始编译: {source_path} ===")
    log(f"日志文件: {_run_log}")
    log(f"Wiki 输出: {WIKI_DIR}")
    started = time.monotonic()

    # ── 统计 ──────────────────────────────────────────────
    files_loaded = 0
    files_converted = 0
    files_chunked = 0
    files_extracted = 0
    pages_created = 0
    skip_errors: list[IngestError] = []

    # ── 0. 初始化 ─────────────────────────────────────────
    log("0. 初始化 LLM + VLM")
    try:
        cfg = load_config(project_root=PROJECT_ROOT)
        llm = create_llm(cfg.llm)
        vlm = create_vlm(cfg.vlm)
    except Exception as e:
        log(f"LLM 初始化失败: {e}", "ERROR"); sys.exit(1)
    log(f"   LLM: {llm.model_id}  |  VLM: {vlm.model_id}")

    # 统一队列——失败事项的人机接口。
    # 路径注入（config 解析的 workspace，不重推导——env 覆盖
    # workspace_dir 时与 CLI 指向同一位置）
    from wiki_agent.queue import QueueStore
    queue = QueueStore(cfg.paths.resolved_workspace_dir())

    # ── 1. DataLoader ─────────────────────────────────────
    log("1. DataLoader: 扫描文件")
    loader = DataLoader()
    summary = loader.load_dir(source_path)
    files_loaded = summary.loaded_count
    log(f"   发现: {summary.total_found}  加载: {files_loaded}  跳过: {summary.skipped_count}")
    for entry in summary.skipped[:10]:
        log(f"   ✗ 跳过 {entry.get('name', entry)} — {entry.get('reason', '')}", "WARN")
    if not summary.files:
        log("无文件可处理", "ERROR"); return

    # ── 2-5. 逐文件: 单文件流水线（compile/watch 共用入口）──
    pipeline = CompilePipeline(
        llm=llm, vlm=vlm, wiki_dir=WIKI_DIR,
        chunk_size=CHUNK_SIZE, model_context=120_000,
        extract_concurrency=EXTRACT_CONCURRENCY,
    )

    def _record_failure(stage: IngestStage, source: str, exc: Exception) -> None:
        """边界统一收集失败——异常对象即汇总记录。

        - log()/logger: 给人看（摘要 + exc_info 回溯进 run.log）
        - compile_failure 事件: 给机器看（全量 error/cause/raw——
          机器通道不截断，截断是给人看的习惯；事件流是唯一机器事实源）
        - 统一队列: 待处理事项的人机接口（/queue 列出、处理后移除）
        - skip_errors: 内存列表，只服务汇总打印（人看），不落第二份文件
        """
        err = exc if isinstance(exc, IngestError) else IngestError(
            stage, f"未分类: {exc}", source=source, cause=exc)
        log(f"  ✗ {err.stage.value}: {err}", "ERROR")
        _boundary_logger.error(
            "编译失败 [%s] %s: %s", err.stage.value, source, str(err)[:200],
            exc_info=err.cause if err.cause else err,
        )
        emit_event("compile_failure", stage=err.stage.value,
                   file=err.source, error=str(err),
                   cause=type(err.cause).__name__ if err.cause else None,
                   raw=err.raw)
        queue.append("ingest_failure",
                     source="compile", file=err.source,
                     stage=err.stage.value, error=str(err)[:500])
        skip_errors.append(err)

    def _save_artifact(artifact_dir: Path, name: str, content: str) -> None:
        (artifact_dir / name).write_text(content, encoding="utf-8")

    for idx, raw_file in enumerate(summary.files):
        file_no = f"[{idx + 1}/{files_loaded}]"
        log(f"{file_no} {raw_file.name}")

        # 本文件的产物目录——原始材料按源文件归拢，审计/调试可回溯
        artifact_dir = ARTIFACTS_DIR / raw_file.name
        artifact_dir.mkdir(parents=True, exist_ok=True)

        try:
            outcome = await pipeline.ingest_one(raw_file)
        except IngestError as e:
            _record_failure(e.stage, raw_file.name, e); continue

        files_converted += 1
        if outcome.extract:
            files_extracted += 1
            _save_artifact(artifact_dir, "extract.json",
                           outcome.extract.document_summary)
        if outcome.plan:
            _save_artifact(artifact_dir, "plan.json", json.dumps(
                {"source": raw_file.name, "page_targets": [
                    {"wiki_path": pt.wiki_path, "title": pt.title,
                     "disposition": pt.disposition.value, "reason": pt.reason}
                    for pt in outcome.plan.page_targets
                ], "raw": outcome.plan.raw}, ensure_ascii=False, indent=2))
            n = len(outcome.plan.page_targets)
            log(f"  {n} 个受影响页面")
            for pt in outcome.plan.page_targets:
                log(f"    [{pt.disposition.value}] {pt.wiki_path} — {pt.title}")
                log(f"      reason: {pt.reason}")
            if n == 0:
                emit_event("plan_noop", file=raw_file.name,
                           reason="page_targets 为空")
                continue

        # 页面副本存档
        pages_log_dir = artifact_dir / "pages"
        pages_log_dir.mkdir(exist_ok=True)
        for wiki_path in outcome.pages_written:
            full = WIKI_DIR / wiki_path
            page_content = full.read_text(encoding="utf-8")
            pages_created += 1
            log(f"    ✓ {wiki_path} ({len(page_content)} chars)")
            safe_name = wiki_path.replace("/", "_")
            (pages_log_dir / safe_name).write_text(
                f"# generated at: {datetime.now().isoformat()}\n\n{page_content}",
                encoding="utf-8",
            )

    # ── 6. 汇总 ──────────────────────────────────────────
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
            by_stage[err.stage].append(err)
        for stage, items in by_stage.items():
            log(f"  [{stage.value}] {len(items)} 个文件:")
            for item in items:
                log(f"    - {item.source}: {item}", "ERROR")
        # 机器侧失败数据已在 events.jsonl（compile_failure 全量
        # error/cause/raw）——不落第二份清单，避免双真相源漂移。
        # 未来"重试失败文件"工具直接过滤事件流。
        # 汇总建议
        retry_count = sum(
            1 for s in skip_errors
            if s.cause is not None and "未分类" not in str(s)
        )
        log(f"  建议: 其中 {retry_count} 个失败可检查日志后重试该文件，"
            f"未分类失败请排查代码")

    # ── 7. Wiki 质量扫描（后处理兜底）──────────────────
    log("\n=== Wiki 质量扫描 ===")
    issues = scan_wiki(WIKI_DIR)
    error_issues = [i for i in issues if i.level == "error"]
    warn_issues = [i for i in issues if i.level == "warning"]
    log(f"   扫描结果: {len(error_issues)} 错误, {len(warn_issues)} 警告")
    for i in issues:
        level = "ERROR" if i.level == "error" else "WARN"
        log(f"   {str(i)}", level)
    # 扫描报告进 run 目录（人看结论）
    (RUN_DIR / "scan_report.md").write_text(
        format_scan_report(issues), encoding="utf-8")

    # 输出 wiki 目录树
    log("\nwiki/ 目录结构:")
    for root, dirs, files in os.walk(WIKI_DIR):
        lvl = root.replace(str(WIKI_DIR), "").count(os.sep)
        ind = "  " * lvl
        log(f"{ind}{os.path.basename(root) or 'wiki'}/")
        for f in sorted(files)[:20]:
            log(f"{ind}  {f}")
        if len(files) > 20:
            log(f"{ind}  ... 共 {len(files)} 个文件")


# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <源文件夹路径>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
