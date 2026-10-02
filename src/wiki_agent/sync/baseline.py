"""同步基线——磁盘与已处理状态的对比判定。

jobs 提交口只经 Baseline 协议消费这里的结果（落后集合、快照差集），
不 import sync 内部，避免 jobs 与 sync 包级互相依赖。
接线由装配根（runtime/helpers）完成。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.issues import IssueKind, IssueStatus
from wiki_agent.sync.state import SyncState, scan_disk

if TYPE_CHECKING:
    from wiki_agent.issues import IssueStore


class SyncBaseline:
    """磁盘与已处理状态的一致性判定；待处理失败记录（open/blocked）不算落后。"""

    def __init__(self, *, state: SyncState, issues: IssueStore, materials_dir: str | Path):
        self.state = state  # 与装配根/测试共享同一实例，不要另建
        self._issues = issues
        self._materials_dir = Path(materials_dir)

    def lagging_sources(self) -> set[str]:
        """落后集合 = 脏文件 − 已有待处理失败记录的文件，返回绝对路径。

        失败过的源已有 issue 记录待用户处理，不再阻塞批操作。
        """
        disk = scan_disk(self._materials_dir)
        dirty, _removed = self.state.diff(disk)
        quarantined = {
            str(record.context.get("source_path") or "")
            for record in self._issues.list(
                statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
                kinds={IssueKind.INGESTION_FAILURE},
                limit=1000,
            )
        }
        return {str(Path(path).resolve()) for path, _digest in dirty} - quarantined

    def recorded_hashes(self) -> set[str]:
        """有处理记录的绝对路径集合，供判定孤立的失败记录。"""
        return {p for p in self.state.all_paths() if self.state.get(p).hash}

    def inspect(self, source_dir: str | Path) -> tuple[dict[str, str], list[tuple[str, str]], list[str]]:
        """一次磁盘扫描并计算差集，返回 (disk, dirty, removed)。

        disk 供 submit_sync 复用（孤儿失败记录判定依赖它），避免重复扫描。
        """
        disk = scan_disk(Path(source_dir).resolve())
        dirty, removed = self.state.diff(disk)
        return disk, dirty, removed
