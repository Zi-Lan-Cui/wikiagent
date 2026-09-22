"""执行锁与批门——跨进程互斥（flock 进程死自动释放）、同进程重入、在途拒绝。

直接运行:  .venv/bin/python test/test_exec_lock.py
"""

import subprocess
import sys
from pathlib import Path

import pytest

from wiki_agent.exec_lock import (
    ExecutionBusy,
    acquire_execution_lock,
    batch_wiki_transaction,
    release_execution_lock,
)

_CHILD = """
import time
from wiki_agent.exec_lock import ExecutionBusy, acquire_execution_lock
try:
    acquire_execution_lock(sys_path_workspace)
except ExecutionBusy:
    print("busy", flush=True)
else:
    print("acquired", flush=True)
    time.sleep(30)
"""


def _child_verdict(ws: Path) -> str:
    """真实子进程尝试持锁并回报结果——flock 的作用域是进程，同进程测不了互斥。"""
    code = _CHILD.replace("sys_path_workspace", repr(str(ws)))
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        return proc.stdout.readline().strip()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_second_process_refused_then_auto_released(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c", _CHILD.replace("sys_path_workspace", repr(str(ws)))],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "acquired"
        with pytest.raises(ExecutionBusy, match="正持有"):
            acquire_execution_lock(ws)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    # 进程死亡内核自动释放——无需判活、无 stale 清理
    assert _child_verdict(ws) == "acquired"


def test_same_process_reentrant_with_refcount(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    acquire_execution_lock(ws)  # runtime.start 已持有的宿主里再进批壳
    acquire_execution_lock(ws)
    release_execution_lock(ws)  # 引用未归零：进程仍是持有者
    assert _child_verdict(ws) == "busy"
    release_execution_lock(ws)  # 归零真解锁
    assert _child_verdict(ws) == "acquired"
    release_execution_lock(ws)  # 未持有时 release 为 no-op


def test_batch_gate_rejects_in_flight_jobs(tmp_path: Path):
    """同进程门：web 泵正在跑 compile/delete 时批流程拒绝进 wiki。"""
    from wiki_agent.jobs.store import JobStore

    ws = tmp_path / "ws"
    ws.mkdir()
    store = JobStore(ws)
    job = store.enqueue(kind="compile", resource="/x/note.md", mode="sync")
    with pytest.raises(RuntimeError, match="在途"):
        with batch_wiki_transaction(ws):
            pass
    # 队列排空后正常进出（try_finalize 只翻转 running 行，queued 直写终态）
    store.update(job.id, status="succeeded", stage="completed")
    with batch_wiki_transaction(ws):
        pass


def test_wiki_revert_refused_while_job_in_flight(tmp_path: Path):
    import asyncio

    from wiki_agent.agent.commands import CommandContext, WikiCommand
    from wiki_agent.conversation import Session
    from wiki_agent.jobs.service import JobService

    ws = tmp_path / "ws"
    wiki = ws / "wiki"
    wiki.mkdir(parents=True)
    service = JobService(ws)
    service.submit(kind="compile", resource="/x", mode="sync")

    class _ReadFile:
        root = wiki

    class _Registry:
        def get(self, name):
            return _ReadFile() if name == "ReadFile" else None

    class _Agent:
        tool_registry = _Registry()
        job_service = service
        workspace = ws

    ctx = CommandContext(
        raw="/wiki revert deadbeef",
        key="wiki",
        args="revert deadbeef",
        session=Session("t"),
        agent=_Agent(),
    )
    result = asyncio.run(WikiCommand().execute(ctx))
    assert "在途" in (result.text or ""), "revert 入口带 restore，有活即拒"


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        try:
            fn(Path(tempfile.mkdtemp()))
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
