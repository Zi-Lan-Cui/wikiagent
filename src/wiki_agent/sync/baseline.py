"""同步基线面——"磁盘 vs 完成账"的判定归 sync 域。

jobs 提交口只经 Baseline 协议消费这里的事实（落后集合/快照差集），
不 import sync 内部；此前 scan_disk/SyncState 直接长在 jobs/service，
构成 jobs↔sync 的包级环。装配根（runtime/helpers）负责接线。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.issues import IssueKind, IssueStatus
from wiki_agent.sync.state import SyncState, scan_disk

if TYPE_CHECKING:
    from wiki_agent.issues import IssueStore


class SyncBaseline:
    """完成账与磁盘的一致性判定；隔离区（open/blocked 失败账）不算落后。"""

    def __init__(self, *, state: SyncState, issues: IssueStore, materials_dir: str | Path):
        self.state = state  # 装配根与测试可取回同一账本实例（勿造第二个缓存）
        self._issues = issues
        self._materials_dir = Path(materials_dir)

    def lagging_sources(self) -> set[str]:
        """落后集合 = 脏源 − 隔离区，返回 resolve 后的绝对路径。

        隔离区语义：失败即保持脏并挂着等人的账——源已被打账、页面与
        账本一致，不该再卡批操作。
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
        """完成账里有账的绝对路径集——孤儿失败记录判定的账本侧。"""
        return {p for p in self.state.all_paths() if self.state.get(p).hash}

    def inspect(self, source_dir: str | Path) -> tuple[dict[str, str], list[tuple[str, str]], list[str]]:
        """一次磁盘扫描 + 账本差集，返回 (disk, dirty, removed)。

        同一份 disk 供 submit_sync 复用（孤儿失败记录判定依赖它），
        避免批提交里扫两遍盘。
        """
        disk = scan_disk(Path(source_dir).resolve())
        dirty, removed = self.state.diff(disk)
        return disk, dirty, removed
