"""执行锁：同一份 workspace 同时只允许一个 wiki 写者（管理者）。

wiki 写协议（pre-reset/commit/restore）隐含前提是工作树写者唯一：两个泵
并发执行不同 job 时，`commit_all` 会把对方的在途页面卷进自己的提交、
失败路径的 restore 会抹掉对方的半成品。跨进程唯一性不靠约定，靠这把
flock——进程死亡内核自动释放，没有判活、没有 stale 清理入口（与被退役
的 run 容器的 O_EXCL 锁本质不同）。

持有者 = 一切会泵队列的宿主：web runtime.start()、CLI sync/refine/
restructure 自泵脚本、compile_batches 评测编排（批流程已全部入队，
不存在旁路写者）。同进程按引用计数重入：runtime.start() 已持有的宿主里
再执行 /compile 不会自锁。
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

_LOCK_NAME = "exec.lock"
# 进程级登记表：flock 的持有与打开文件描述绑定，跨 fd 的重复加锁会
# 与自身冲突——同进程复用同一 fd 并计数
_HOLDER: dict[Path, int] = {}
_REFCOUNT: dict[Path, int] = {}


class ExecutionBusy(RuntimeError):
    """workspace 已被另一个执行进程持有。"""


def execution_lock_path(workspace: str | Path) -> Path:
    return Path(workspace) / _LOCK_NAME


def acquire_execution_lock(workspace: str | Path) -> None:
    """持有执行锁（同进程重入计数）。已被他进程持有时抛 ExecutionBusy。"""
    path = execution_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path in _HOLDER:
        _REFCOUNT[path] += 1
        return
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        holder = ""
        try:
            holder = path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        raise ExecutionBusy(
            f"另一执行进程（{holder or 'pid 未知'}）正持有 {path.name}——"
            "wiki 写者必须唯一；请用它，或先结束那个进程"
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    _HOLDER[path] = fd
    _REFCOUNT[path] = 1


def release_execution_lock(workspace: str | Path) -> None:
    """释放一次持有；引用归零时真正解锁。未持有时为 no-op。"""
    path = execution_lock_path(workspace)
    fd = _HOLDER.get(path)
    if fd is None:
        return
    _REFCOUNT[path] -= 1
    if _REFCOUNT[path] > 0:
        return
    _HOLDER.pop(path, None)
    _REFCOUNT.pop(path, None)
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
