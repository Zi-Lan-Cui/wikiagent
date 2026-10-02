"""执行锁：同一 workspace 同时只允许一个 wiki 写者。

wiki 写协议（pre-reset/commit/restore）要求写者唯一：两个任务并发执行时，
`commit_all` 会把对方未提交的页面带进自己的提交，失败路径的 restore 会覆盖
对方写到一半的文件。跨进程唯一性由 flock 保证：进程退出时内核自动释放，
无需检测持有进程存活，也没有过期锁需要清理。

持有者为所有驱动任务队列的进程：web 的 runtime.start()、批处理脚本。
同进程按引用计数重入：已持有锁的进程内再次获取不会自锁。
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

_LOCK_NAME = "exec.lock"
# flock 与打开的文件描述符绑定，同进程再次 open 加锁会与自身冲突，
# 故按路径复用同一 fd 并计数
_HOLDER: dict[Path, int] = {}
_REFCOUNT: dict[Path, int] = {}


class ExecutionBusy(RuntimeError):
    """workspace 的执行锁已被另一个进程持有。"""


def execution_lock_path(workspace: str | Path) -> Path:
    return Path(workspace) / _LOCK_NAME


def acquire_execution_lock(workspace: str | Path) -> None:
    """获取执行锁；同进程重入时累加计数。锁被他进程持有时抛 ExecutionBusy。"""
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
    """释放一次持有；引用计数归零时真正解锁。未持有时不做任何事。"""
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
