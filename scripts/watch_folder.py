"""watch 模式入口——监视源文件夹，大改动自动重新 ingest。

用法:
    uv run python scripts/watch_folder.py /path/to/source-folder

数据流:
    源目录 → 变更监视（事件驱动，去抖+稳定性确认，定时全量扫描兜底）
           → 持久 Job 队列 → Worker（串行） → 编译流水线
           → 成功核账写 watch state；失败进 issue 重试通道

装配全部走 AppRuntime（与 web 同一组合根）——本脚本只加 watcher 与
运行容器，不再手工接线。
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from wiki_agent.application.runtime import AppRuntime
from wiki_agent.config import load_config
from wiki_agent.log import begin_trace, configure_logging, get_logger, setup_event_log
from wiki_agent.watch.watcher import FileWatcher

logger = get_logger("WATCH_FOLDER")


async def main(source_dir: str | None = None):
    """watch 主流程——初始化 → 启动 reconcile → 常驻运行。

    Args:
        source_dir: 源文件夹路径。
    """
    cfg = load_config(project_root=PROJECT_ROOT)
    source_path = (
        Path(source_dir).expanduser().resolve()
        if source_dir is not None
        else cfg.paths.resolved_materials_dir().resolve()
    )
    if not source_path.is_dir():
        # configure_logging 尚未执行（run 目录还没建）——WARNING 走 logging
        # 的 lastResort handler 仍会到 stderr，退出码 1 供编排判断。
        logger.error("源目录不存在: %s", source_path)
        sys.exit(1)

    # 运行目录（与 compile 一致的 run 容器）
    run_dir = cfg.paths.resolved_runs_dir() / f"watch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(console_level=logging.INFO, file_path=str(run_dir / "run.log"))
    setup_event_log(run_dir / "events.jsonl")
    # 进程级 trace——watch 是常驻进程，一个 trace 贯穿整个会话
    begin_trace(trace_id=f"watch_{run_dir.name}")

    runtime = AppRuntime.from_project_root(PROJECT_ROOT)
    watcher = FileWatcher(
        source_path,
        runtime.watch_state,
        wiki_dir=runtime.wiki_dir,
        settle_window=cfg.watch.settle_window,
        stability_delay=cfg.watch.stability_delay,
        fallback_interval=cfg.watch.fallback_interval,
        submit_job=lambda resource, deleted, digest: runtime.job_service.submit_watch_change(
            resource, deleted=deleted, digest=digest
        ),
    )

    logger.info("watch 模式启动: %s", source_path)
    logger.info("检测: 事件驱动（去抖+稳定性复读） + 周期全量扫描兜底")
    logger.info("事件流: %s", run_dir / "events.jsonl")

    async with runtime:  # 拉起 worker 与后台对账循环
        await watcher.run()  # 首轮全量对账在 run 内（无变更则静默）


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
    except KeyboardInterrupt:
        logger.info("watch 停止")
