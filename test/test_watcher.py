"""事件驱动 watcher——事件路径（去抖+稳定性+变更门）+ 回退路径（删除检测）。

直接运行:  .venv/bin/python test/test_watcher.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.watch.state import WatchState
from wiki_agent.watch.watcher import FileWatcher


def _make_env(tmp: Path):
    src = tmp / "src"
    src.mkdir()
    state_path = tmp / "state.json"
    queue: asyncio.Queue = asyncio.Queue()
    return src, WatchState(state_path), queue


def _new_file(src: Path, name: str = "note.md", content: str = "hello") -> Path:
    p = src / name
    p.write_text(content, encoding="utf-8")
    return p


def test_event_path_change_detection():
    """事件路径: 文件变化 → settle → 稳定性复读 → 入队（大改动）。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        src, state, queue = _make_env(tmp)
        watcher = FileWatcher(
            src, queue, state, settle_window=0.1, stability_delay=0.1, fallback_interval=3600
        )
        # 先建立已知状态（新文件两段确认需 2 轮扫描）
        f = _new_file(src, content="原文内容" * 20)
        await watcher._poll_once()
        await watcher._poll_once()
        queue.get_nowait()  # 首次入队（建立已知状态）

        # 大改动（相似度远低于阈值）
        f.write_text("完全不同的新内容" * 30, encoding="utf-8")
        # 事件路径: _notify → settle → 检查
        watcher._loop = asyncio.get_running_loop()
        watcher._notify(str(f))
        await asyncio.sleep(0.5)  # settle 0.1 + stability 0.1 + 余量
        # 大改动应入队
        assert not queue.empty(), "大改动应入队"
        item = queue.get_nowait()
        assert str(item) == str(f)

    asyncio.run(run())


def test_event_path_micro_change_skipped():
    """事件路径: 微调（相似度高）跳过变更门。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        src, state, queue = _make_env(tmp)
        watcher = FileWatcher(
            src, queue, state, settle_window=0.1, stability_delay=0.1, fallback_interval=3600
        )
        base = "这是关于迭代器的基础内容" * 15
        f = _new_file(src, content=base)
        await watcher._poll_once()  # 建立已知状态（两段确认 2 轮）
        await watcher._poll_once()
        queue.get_nowait()  # 清掉首次入队

        # 微调: 改一个词（相似度极高）
        f.write_text(base.replace("基础", "基本"), encoding="utf-8")
        watcher._loop = asyncio.get_running_loop()
        watcher._notify(str(f))
        await asyncio.sleep(0.5)
        assert queue.empty(), "微调应被变更门跳过"

    asyncio.run(run())


def test_fallback_path_delete_detection():
    """回退路径: 文件删除 → state drop + delete 事件入队。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        src, state, queue = _make_env(tmp)
        watcher = FileWatcher(src, queue, state, fallback_interval=3600)
        f = _new_file(src, "note.md", "内容")
        await watcher._poll_once()
        await watcher._poll_once()
        queue.get_nowait()  # 首次入队

        f.unlink()
        queued = await watcher._poll_once()
        assert any(q.startswith("delete:note.md") for q in queued)
        item = queue.get_nowait()
        assert item[0] == "delete" and item[1] == "note.md"

    asyncio.run(run())


def test_event_path_delete_detection():
    """事件路径: 文件消失 → settle 检查发现 → delete 入队。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        src, state, queue = _make_env(tmp)
        watcher = FileWatcher(
            src, queue, state, settle_window=0.1, stability_delay=0.1, fallback_interval=3600
        )
        f = _new_file(src, "note.md", "内容")
        await watcher._poll_once()
        await watcher._poll_once()
        queue.get_nowait()

        watcher._loop = asyncio.get_running_loop()
        f.unlink()
        # 真实 watchdog 会发事件；这里直接走事件路径的检查
        watcher._notify(str(f))
        await asyncio.sleep(0.5)
        item = queue.get_nowait()
        assert item[0] == "delete" and item[1] == "note.md"

    asyncio.run(run())


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
