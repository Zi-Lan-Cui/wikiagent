"""FileWatcher 纯生产者契约——确认只提交 Job，不写完成账。

覆盖: 事件路径提交/微调跳过/删除、回退路径两段确认、
提交不改 state.hash（I4）、delete 条目延迟清除（可重发现）。

直接运行:  .venv/bin/python test/test_watcher.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.watch.state import WatchState, digest_file_text
from wiki_agent.watch.watcher import FileWatcher


def _make_env(tmp: Path):
    src = tmp / "src"
    src.mkdir()
    state = WatchState(tmp / "watch" / "state.json")
    submits: list[tuple[str, bool, str]] = []
    watcher = FileWatcher(
        src,
        state,
        submit_job=lambda resource, deleted, digest: submits.append((resource, deleted, digest)),
        settle_window=0.05,
        stability_delay=0.05,
        fallback_interval=3600,
    )
    return src, state, submits, watcher


def _new_file(src: Path, content: str) -> Path:
    f = src / "note.md"
    f.write_text(content, encoding="utf-8")
    return f


def test_event_path_submits_without_marking_hash(tmp_path: Path):
    """大改动经 settle+稳定性 → 提交带 digest；state.hash 不动（I4）。"""

    async def run():
        src, state, submits, watcher = _make_env(tmp_path)
        f = _new_file(src, content="原文内容" * 20)
        watcher._loop = asyncio.get_running_loop()

        watcher._notify(str(f))
        await asyncio.sleep(0.4)

        assert len(submits) == 1
        resource, deleted, digest = submits[0]
        assert resource == str(f) and deleted is False
        assert len(digest) == 64, "提交必须携带内容指纹"
        assert state.get(str(f)).hash == "", "提交不得写完成账"
        assert state.get(str(f)).pending_text is None, "定案应清两段确认现场"

    asyncio.run(run())


def test_event_path_micro_change_skipped(tmp_path: Path):
    """微调（相似度高）跳过变更门——不提交。"""

    async def run():
        src, state, submits, watcher = _make_env(tmp_path)
        base = "这是关于迭代器的基础内容" * 15
        f = _new_file(src, content=base)
        await watcher._poll_once()  # 两段确认第一轮
        await watcher._poll_once()  # 定案提交
        digest, text = digest_file_text(f)
        state.record(str(f), digest, text)  # 模拟成功核账完成
        submits.clear()

        f.write_text(base.replace("基础", "基本"), encoding="utf-8")
        watcher._loop = asyncio.get_running_loop()
        watcher._notify(str(f))
        await asyncio.sleep(0.4)
        assert submits == [], "微调应被变更门跳过"

    asyncio.run(run())


def test_fallback_two_phase_confirmation(tmp_path: Path):
    """回退路径新文件: 第一轮只 pending 不提交，第二轮定案提交。"""

    async def run():
        src, state, submits, watcher = _make_env(tmp_path)
        f = _new_file(src, content="轮询确认内容" * 10)

        await watcher._poll_once()
        assert submits == [], "第一段确认不应提交"
        assert state.get(str(f)).pending_seen == 1

        await watcher._poll_once()
        assert len(submits) == 1 and submits[0][0] == str(f)
        assert state.get(str(f)).hash == "", "回退提交同样不落完成账"

        submits.clear()
        await watcher._poll_once()
        assert submits == [], "同内容第三轮不再提交"

    asyncio.run(run())


def test_fallback_delete_detection_keeps_state_entry(tmp_path: Path):
    """删除检测: 提交 delete job（resource=绝对路径、digest 空）；条目保留待成功核账清除。"""

    async def run():
        src, state, submits, watcher = _make_env(tmp_path)
        f = _new_file(src, content="将被删除" * 10)
        await watcher._poll_once()
        await watcher._poll_once()
        submits.clear()
        f.unlink()

        await watcher._poll_once()
        assert submits == [(str(f), True, "")]
        assert str(f) in state.all_paths(), "delete 成功前条目必须留存（可重发现）"

        submits.clear()
        await watcher._poll_once()
        assert submits == [(str(f), True, "")], "在途期间重复提交由幂等吸收"

    asyncio.run(run())


def test_event_path_delete_detection(tmp_path: Path):
    """事件路径发现消失 → 提交 delete。"""

    async def run():
        src, state, submits, watcher = _make_env(tmp_path)
        f = _new_file(src, content="事件删除" * 10)
        await watcher._poll_once()
        await watcher._poll_once()
        submits.clear()
        f.unlink()

        watcher._loop = asyncio.get_running_loop()
        watcher._notify(str(f))
        await asyncio.sleep(0.3)
        assert submits == [(str(f), True, "")]

    asyncio.run(run())


def test_unchanged_touch_ignored(tmp_path: Path):
    """完成账在手、内容不变 → 事件与扫描都不提交。"""

    async def run():
        src, state, submits, watcher = _make_env(tmp_path)
        f = _new_file(src, content="稳定内容" * 10)
        await watcher._poll_once()
        await watcher._poll_once()
        digest, text = digest_file_text(f)
        state.record(str(f), digest, text)
        submits.clear()

        f.write_text("稳定内容" * 10, encoding="utf-8")
        await watcher._poll_once()
        watcher._loop = asyncio.get_running_loop()
        watcher._notify(str(f))
        await asyncio.sleep(0.3)
        assert submits == []

    asyncio.run(run())


if __name__ == "__main__":
    import traceback

    failed = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t(Path(tempfile.mkdtemp()))
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
