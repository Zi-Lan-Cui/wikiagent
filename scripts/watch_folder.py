"""watch 模式入口——监视源文件夹，大改动自动重新 ingest。

用法:
    VIRTUAL_ENV= .venv/bin/python scripts/watch_folder.py /media/.../Python

数据流:
    源目录 → FileWatcher(事件驱动 inotify：去抖+稳定性复读+变更门，
             60s 回退全量扫描兜底) → asyncio.Queue
           → WatchConsumer(单 worker 串行) → CompilePipeline.ingest_one
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# 项目 src 加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.compiler.pipeline import CompilePipeline
from wiki_agent.log import begin_trace, configure_logging, emit_event, setup_event_log
from wiki_agent.watch.consumer import WatchConsumer
from wiki_agent.watch.state import WatchState
from wiki_agent.watch.watcher import FileWatcher

WIKI_DIR = PROJECT_ROOT / "wiki"


async def main(source_dir: str):
    source_path = Path(source_dir).resolve()
    if not source_path.is_dir():
        print(f"源目录不存在: {source_dir}")
        sys.exit(1)

    # ── 运行目录（与 compile 一致的 run 容器）────────────────
    run_dir = WIKI_DIR / ".logs" / "runs" / f"watch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(file_path=str(run_dir / "run.log"))
    setup_event_log(run_dir / "events.jsonl")
    # 进程级 trace——watch 是常驻进程，一个 trace 贯穿整个会话
    begin_trace(trace_id=f"watch_{run_dir.name}")

    # ── 初始化 ─────────────────────────────────────────────
    cfg = load_config(project_root=PROJECT_ROOT)
    llm = create_llm(cfg.llm)
    vlm = create_vlm(cfg.vlm)

    pipeline = CompilePipeline(llm=llm, vlm=vlm, wiki_dir=WIKI_DIR)
    state = WatchState(WIKI_DIR / ".watch" / "state.json")
    queue: asyncio.Queue = asyncio.Queue()

    watcher = FileWatcher(source_path, queue, state, wiki_dir=WIKI_DIR)
    consumer = WatchConsumer(queue, pipeline, state, wiki_dir=WIKI_DIR)

    print(f"watch 模式启动: {source_path}")
    print("检测: inotify 事件驱动（去抖 2s + 稳定性复读 2s）"
          " + 60s 回退全量扫描兜底")
    print(f"事件流: {run_dir / 'events.jsonl'}")

    # 启动 reconcile: 先扫一轮存量变化（无变更则静默）
    await watcher._poll_once()

    await asyncio.gather(watcher.run(), consumer.run())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <源文件夹路径>")
        sys.exit(1)
    try:
        asyncio.run(main(sys.argv[1]))
    except KeyboardInterrupt:
        print("\nwatch 停止")
