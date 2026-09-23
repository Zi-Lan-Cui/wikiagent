"""源文件快照——提交时复制输入，任务执行只读这份副本。

三条不变量：

    快照是输入；job 不读快照以外的源文件状态；issue 只随结算更新。

布局：``workspace/snapshots/<batch_id>/<相对源目录的路径>``。
一批一个目录；复制保相对路径，文件名与原件一致；写入用临时名替换就位，
崩溃时不会留下半文件。批的最后一个任务进入终态时删目录；进程启动时
清扫"没有对应非终态任务"的遗留目录——没有保留期配置，没有后台回收。

不做内容寻址去重：批间互斥串行保证同一时刻一份内容至多属于一个活跃批，
去重收益为零而多一套清单真相源。manifest 不需要：jobs 行的
payload.batch/digest 就是本批清单。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

from wiki_agent.log import get_logger
from wiki_agent.sync.state import digest_file_text

logger = get_logger("SNAPSHOTS")

# 快照根目录名——workspace 下的内部布局，不是配置项
SNAPSHOTS_DIRNAME = "snapshots"


class SnapshotError(RuntimeError):
    """快照读写基础设施故障（磁盘、权限、损坏）——不是源文件的业务失败。"""


class SnapshotStore:
    """一个 workspace 一份快照仓库；service（写入）与 consumer（读取）共享。"""

    def __init__(self, workspace: str | Path):
        self.root = Path(workspace) / SNAPSHOTS_DIRNAME

    def capture(self, batch_id: str, source_dir: str | Path, paths: Sequence[str | Path]) -> dict[str, str]:
        """按相对路径复制进批目录，返回 原始绝对路径 → 快照件 digest。

        digest 用落盘副本重算（read_text+sha256 唯一配方，与完成账同一函数）：
        复制期间源文件被改动时，记录的也是实际存下的那份内容。
        """
        src_root = Path(source_dir).resolve()
        captured: dict[str, str] = {}
        for item in paths:
            original = Path(item).resolve()
            try:
                rel = original.relative_to(src_root)
            except ValueError as exc:
                raise SnapshotError(f"源文件不在源目录内: {original} ({src_root})") from exc
            staged = self._staged(batch_id, rel)
            staged.parent.mkdir(parents=True, exist_ok=True)
            tmp = staged.with_name(staged.name + ".copying")
            try:
                shutil.copyfile(original, tmp)
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                raise SnapshotError(f"快照复制失败: {original}") from exc
            tmp.replace(staged)
            read = digest_file_text(staged)
            if read is None:
                raise SnapshotError(f"快照写入后不可读: {staged}")
            captured[str(original)] = read[0]
        return captured

    def staged_path(self, batch_id: str, rel_path: str) -> Path:
        """执行入口：payload 里的 batch+rel_path 定位快照件。"""
        return self._staged(batch_id, rel_path)

    def drop_batch(self, batch_id: str) -> None:
        directory = self.root / batch_id
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)

    def sweep_orphans(self, live_batches: Iterable[str]) -> int:
        """删除没有非终态任务引用的批目录（崩溃/中断遗留），返回删除数。"""
        if not self.root.is_dir():
            return 0
        live = set(live_batches)
        removed = 0
        for directory in self.root.iterdir():
            if directory.is_dir() and directory.name not in live:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("清扫无主快照目录 %d 个", removed)
        return removed

    def _staged(self, batch_id: str, rel: str | Path) -> Path:
        return self.root / batch_id / rel
