"""sync 线提交入口：以快照为准的整批入队与状态统计。

互斥闸：写 wiki 的四类任务任一在途即拒——遇在途 compile/delete 抛
SyncInProgress，遇 restructure/link 抛 PipelineBusy；无脏无删的空跑不入队、
不受此限。闸判定与入队同一 IMMEDIATE 事务，在途计数在事务开始时取定。
消失源失败记录经 issues 线的 close_vanished_failures 在同一提交事务内收口，
resolution 形状由 issues 单点维护。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from wiki_agent.issues import IssueStore
from wiki_agent.queue import WIKI_WRITE_KINDS, Job, Kind, PipelineBusy, SyncInProgress
from wiki_agent.queue.queue import JobQueue
from wiki_agent.snapshots import SnapshotStore
from wiki_agent.sync.baseline import SyncBaseline


class SyncCoordinator:
    """提交口判闸与入队；执行体在 source_jobs，状态在 state。"""

    def __init__(
        self,
        *,
        queue: JobQueue,
        issues: IssueStore,
        snapshots: SnapshotStore,
        baseline: SyncBaseline,
    ):
        self.queue = queue
        self.issues = issues
        self.snapshots = snapshots
        self.baseline = baseline

    def _sync_blocked(self, busy: dict[str, int]) -> PipelineBusy:
        """sync 遇在途 compile/delete 抛 SyncInProgress，其余在途抛 PipelineBusy。"""
        if Kind.COMPILE in busy or Kind.DELETE in busy:
            return SyncInProgress()
        return JobQueue.pipeline_busy(busy)

    def submit_sync(self, source_dir: str | Path) -> list[Job]:
        """快照同步：以点击时的磁盘与完成记录之差为本批，脏文件复制为快照后整批入队。

        - 输入是点击时复制到 workspace/snapshots/<batch>/ 的副本；执行期间原件的
          改动、删除、恢复都不影响本批，改动留到下一次 sync。payload.digest 取
          副本的实际内容。
        - 失败不写完成记录即保持脏，再次 sync 即重试；脏文件若有关联 open 失败
          记录，入队时挂 issue_id，重试成功后一并解决。
        - payload.batch 三个用途：快照目录名、wiki commit 批标识（按它定位整批
          撤销）、批内最后一个任务终态时删除目录的分组键。

        先复制后入队，保证任务存在则输入必在；入队失败或被拒时删除刚复制的目录，
        崩溃遗留由队列构造期清扫。
        """
        root = Path(source_dir).resolve()
        disk, dirty, removed = self.baseline.inspect(root)
        if not dirty and not removed:
            # 无任务时也要检查：源已消失的失败记录只依赖磁盘与完成记录判定
            with self.queue.store.transaction(immediate=True) as conn:
                self.issues.close_vanished_failures(
                    disk, self.baseline.recorded_hashes(), _conn=conn
                )
            return []
        busy = self.queue.in_flight_wiki_write_counts()
        if busy:
            raise self._sync_blocked(busy)
        batch = f"sync_{uuid4().hex}"
        try:
            digests = (
                self.snapshots.capture(batch, root, [path for path, _ in dirty]) if dirty else {}
            )
            jobs: list[Job] = []
            with self.queue.store.transaction(immediate=True) as conn:
                busy = self.queue.in_flight_wiki_write_counts(_conn=conn)
                if busy:
                    raise self._sync_blocked(busy)
                for path, _disk_digest in dirty:
                    original = str(Path(path).resolve())
                    pending = self.issues.find_pending_failures(original)
                    jobs.append(
                        self.queue.store.enqueue(
                            kind=Kind.COMPILE,
                            resource=original,
                            mode="sync",
                            payload={
                                "deleted": False,
                                "digest": digests[original],
                                "batch": batch,
                                "rel_path": str(Path(original).relative_to(root)),
                            },
                            idempotency_key=f"{batch}:{original}",
                            issue_id=pending[0].id if pending else "",
                            _conn=conn,
                        )
                    )
                for path in removed:
                    # delete 同 compile：来源有关联的活动失败记录就挂 issue_id
                    pending = self.issues.find_pending_failures(path)
                    jobs.append(
                        self.queue.store.enqueue(
                            kind=Kind.DELETE,
                            resource=path,
                            mode="sync",
                            payload={"deleted": True, "digest": "", "batch": batch},
                            idempotency_key=f"{batch}:{path}",
                            issue_id=pending[0].id if pending else "",
                            _conn=conn,
                        )
                    )
                self.issues.close_vanished_failures(
                    disk, self.baseline.recorded_hashes(), _conn=conn
                )
        except BaseException:
            self.snapshots.drop_batch(batch)
            raise
        return jobs

    def sync_status(self, source_dir: str | Path) -> dict[str, int]:
        """只读统计 dirty/removed 与在途数，不提交。"""
        _disk, dirty, removed = self.baseline.inspect(source_dir)
        return {
            "dirty": len(dirty),
            "removed": len(removed),
            "in_flight": self.queue.store.in_flight_for_kinds(WIKI_WRITE_KINDS),
        }

