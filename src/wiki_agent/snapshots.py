"""源文件快照：任务提交时复制输入文件，执行只读这份副本。

三条不变量：

    快照是输入；job 不读快照以外的源文件状态；issue 只随结算更新。

布局：``workspace/snapshots/<batch_id>/<相对源目录的路径>``。
一批一个目录；复制保持相对路径与文件名；写入先落临时名再替换就位，
进程崩溃不会留下半截文件。批的最后一个任务进入终态时删除目录；
进程启动时清扫没有非终态任务引用的遗留目录。

不做内容寻址去重：批间互斥串行，同一份内容至多属于一个活跃批，
去重没有收益。manifest 不需要：jobs 行的 payload.batch/digest
已记录本批文件清单。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

from wiki_agent.log import get_logger

logger = get_logger("SNAPSHOTS")

def digest_file_text(path: str | Path) -> tuple[str, str] | None:
    """计算内容指纹：read_text(errors=replace) + sha256。

    sync 判断变更与 consumer 记录完成状态必须调用同一函数，两侧各自
    实现会产生对不上的 digest，无法确认完成。
    读失败（文件不存在、无权限）返回 None。
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), text


# workspace 内部布局，不通过配置项暴露
SNAPSHOTS_DIRNAME = "snapshots"


class SnapshotError(RuntimeError):
    """快照读写的基础设施故障（磁盘、权限、文件损坏），不属于业务失败。"""


class SnapshotStore:
    """一个 workspace 一份快照仓库；service（写入）与 consumer（读取）共用。"""

    def __init__(self, workspace: str | Path):
        self.root = Path(workspace) / SNAPSHOTS_DIRNAME

    def capture(self, batch_id: str, source_dir: str | Path, paths: Sequence[str | Path]) -> dict[str, str]:
        """按相对路径复制进批目录，返回 原始绝对路径 → 快照件 digest。

        digest 用落盘副本经 digest_file_text 重新计算：复制期间源文件被
        改动时，记录的是实际存下的那份内容。
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
        """按 payload 中的 batch 与相对路径定位快照文件。"""
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
