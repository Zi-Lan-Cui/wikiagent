"""持久化任务的统一提交与生命周期接口。

Job 是唯一执行事实来源：全部提交入口在这里；终态写入只有一个点，
即 complete_with_outcome，jobs 行与 issue 联动同事务、带 CAS。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from wiki_agent.issues import IssueKind, IssueService, IssueStatus, IssueStore
from wiki_agent.jobs import (
    DuplicateInFlightJob,
    Job,
    JobResult,
    JobStore,
    Kind,
    RestructureInProgress,
    SyncInProgress,
)
from wiki_agent.jobs.outcomes import JobOutcomeHandler
from wiki_agent.jobs.retry_source import SourceUnavailableError, resolve_retry_source
from wiki_agent.snapshots import SnapshotError, SnapshotStore
from wiki_agent.sync.state import SyncState, scan_disk


class JobService:
    """一切可执行工作的提交口与生命周期入口。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        outcomes: JobOutcomeHandler | None = None,
        sync_state: SyncState | None = None,
        wiki_dir: str | Path | None = None,
        source_records_dir: str | Path | None = None,
        snapshots: SnapshotStore | None = None,
    ):
        self.workspace = Path(workspace)
        # issue retry 解析重试输入需要 wiki 根（wiki_page 型资源的定位）
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else None
        # sync 快照对比需要完成账本
        self.sync_state = sync_state
        # 源文件快照仓库：提交即定格输入（submit 写、consumer 读、终态删）
        self.snapshots = snapshots or SnapshotStore(workspace)
        self.store = JobStore(workspace)
        self.issues = IssueStore(workspace)
        self.issue_service = IssueService(self.issues)
        self.outcomes = outcomes or JobOutcomeHandler(
            self.issues,
            sync_state=sync_state,
            source_records_dir=source_records_dir,
        )
        self.recovered_jobs = self.store.recover_stale()
        # 崩溃/中断遗留的无主快照目录在构造期清扫（保留名单=非终态任务引用的批）
        self.snapshots.sweep_orphans(self.store.in_flight_batch_ids())

    # 提交

    def submit(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        issue_id: str = "",
        _conn: sqlite3.Connection | None = None,
    ) -> Job:
        return self.store.enqueue(
            kind=kind,
            resource=resource,
            mode=mode,
            payload=payload,
            idempotency_key=idempotency_key,
            issue_id=issue_id,
            _conn=_conn,
        )

    def submit_issue_retry(self, issue_id: str) -> Job:
        """重试请求 → compile Job：点击时捕获快照输入。

        执行唯一性依次经三层检查收敛：先查该 issue 是否已有在途挂账 job
        （有则直接返回）；再按幂等键命中在途行返回既有；撞唯一在途索引
        （他人占位同一资源）返回占位者、其无账则补挂。retry 资格
        （状态、来源可读）由各入口的 validate 判定，这里只管执行唯一性。
        与 submit_sync 同一规则：digest 来自点击时复制的快照件，
        点击后文件再变，本次重试处理的仍是定格的这份。issue 终态由
        compile job 的 outcome 落，提交本身不改变 issue 状态。
        """
        if self.wiki_dir is None:
            raise RuntimeError("submit_issue_retry 需要 wiki_dir")
        issue = self.issues.require(issue_id)
        source = resolve_retry_source(issue, self.wiki_dir)
        resource = str(Path(source).resolve())
        batch = f"retry_{uuid4().hex}"
        try:
            digest = self.snapshots.capture(batch, Path(resource).parent, [resource])[resource]
        except SnapshotError as exc:
            raise SourceUnavailableError(f"重试输入不可读: {exc}") from exc
        try:
            with self.store.database.transaction(immediate=True) as conn:
                existing = self.store.in_flight_job_by_issue(issue_id, _conn=conn)
                if existing is not None:
                    return existing
                try:
                    return self.store.enqueue(
                        kind=Kind.COMPILE,
                        resource=resource,
                        mode="issue_retry",
                        payload={
                            "deleted": False,
                            "digest": digest,
                            "batch": batch,
                            "rel_path": Path(resource).name,
                        },
                        idempotency_key=f"issue-retry:{issue_id}",
                        issue_id=issue_id,
                        _conn=conn,
                    )
                except DuplicateInFlightJob:
                    occupant = self.store.in_flight_by_resource(resource, _conn=conn)
                    if occupant is None:  # 撞唯一索引必有占位者——防御性外抛
                        raise
                    if not occupant.issue_id:
                        occupant = self.store.attach_issue(occupant.id, issue_id, _conn=conn)
                    return occupant
        finally:
            # 只有"新入队的这一行"用到了本次捕获；收敛到既有行的分支不留无主目录
            current = self.store.in_flight_job_by_issue(issue_id)
            if current is None or current.payload.get("batch") != batch:
                self.snapshots.drop_batch(batch)

    def submit_issue_action(
        self, issue_id: str, action: str, payload: dict[str, object] | None = None
    ) -> Job:
        return self.submit(
            kind=Kind.ISSUE_ACTION,
            resource=issue_id,
            mode=action,
            payload=payload,
            idempotency_key=f"issue-action:{issue_id}:{action}",
            issue_id=issue_id,
        )

    # sync 快照

    def sync_status(self, source_dir: str | Path) -> dict[str, int]:
        """只读快照预演：dirty/removed 计数与在途闸状态——不提交任何东西。"""
        if self.sync_state is None:
            raise RuntimeError("sync_status 需要 sync_state")
        disk = scan_disk(source_dir)
        dirty, removed = self.sync_state.diff(disk)
        return {
            "dirty": len(dirty),
            "removed": len(removed),
            "in_flight": self.store.in_flight_for_kinds((Kind.COMPILE, Kind.DELETE)),
        }

    def submit_sync(self, source_dir: str | Path) -> list[Job]:
        """快照同步：点击时"磁盘 − 账本"之差即本批；脏文件复制定格后整批入队。

        语义契约——"快照是输入"：
        - 互斥串行：compile/delete 有在途则 SyncInProgress，上一批没跑完
          不叠快照；
        - compile 任务的输入是点击时复制进 workspace/snapshots/<批>/
          的副本。执行期间原件修改、删除、复活都不影响本批；改动归下一次
          点击。payload.digest 就是副本的实际内容；
        - 失败不写账即保持脏，再次 sync 就是重试；脏文件若背着 open 失败账
          则同时挂账 issue_id，成功即解决；
        - payload.batch 有三个用途：快照目录名、wiki commit 尾注（撤销整批 =
          按尾注在历史中选段 revert）、批内最后一个任务终态时删目录的分组键。
        顺序：先复制、后入队，任务存在则输入必在；反序会出现任务读不到
        输入的窗口。入队失败或被互斥拒绝时删除刚复制的目录；崩溃遗留由
        JobService 构造期清扫。
        """
        if self.sync_state is None:
            raise RuntimeError("submit_sync 需要 sync_state")
        root = Path(source_dir).resolve()
        disk = scan_disk(root)
        dirty, removed = self.sync_state.diff(disk)
        if not dirty and not removed:
            # 无任务时也要检查：孤儿失败记录的判定只依赖磁盘与完成账本
            with self.store.database.transaction(immediate=True) as conn:
                self._close_vanished_failures(disk, conn)
            return []
        if self.store.in_flight_for_kinds((Kind.COMPILE, Kind.DELETE)) > 0:
            raise SyncInProgress()
        batch = f"sync_{uuid4().hex}"
        try:
            digests = (
                self.snapshots.capture(batch, root, [path for path, _ in dirty]) if dirty else {}
            )
            jobs: list[Job] = []
            with self.store.database.transaction(immediate=True) as conn:
                if self.store.in_flight_for_kinds((Kind.COMPILE, Kind.DELETE), _conn=conn) > 0:
                    raise SyncInProgress()
                for path, _disk_digest in dirty:
                    original = str(Path(path).resolve())
                    pending = self.issues.find_pending_failures(original)
                    jobs.append(
                        self.store.enqueue(
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
                    # delete 与 compile 同一挂账规则：来源有活动失败记录就挂上，
                    # 删除成功（delete_applied）连同同源旧账一起收
                    pending = self.issues.find_pending_failures(path)
                    jobs.append(
                        self.store.enqueue(
                            kind=Kind.DELETE,
                            resource=path,
                            mode="sync",
                            payload={"deleted": True, "digest": "", "batch": batch},
                            idempotency_key=f"{batch}:{path}",
                            issue_id=pending[0].id if pending else "",
                            _conn=conn,
                        )
                    )
                self._close_vanished_failures(disk, conn)
        except BaseException:
            self.snapshots.drop_batch(batch)
            raise
        return jobs

    def _close_vanished_failures(
        self, disk: dict[str, str], _conn: sqlite3.Connection | None = None
    ) -> int:
        """关闭"对象已不存在"的活动失败记录：source 既不在磁盘也不在完成账里，
        它不会再出现在任何任务里。按事实关闭、事件留痕，不靠人工逐条清理。
        """
        assert self.sync_state is not None
        hashed = {p for p in self.sync_state.all_paths() if self.sync_state.get(p).hash}
        closed = 0
        for record in self.issues.list(
            statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
            kinds={IssueKind.INGESTION_FAILURE},
            limit=1000,
        ):
            src = str(record.context.get("source_path") or "")
            if src and src not in disk and src not in hashed:
                self.issues.transition(
                    record.id,
                    IssueStatus.RESOLVED,
                    resolution={"cause": "source_deleted", "closed_by": "submit_sync"},
                    event="issue_source_vanished",
                    _conn=_conn,
                )
                closed += 1
        return closed

    # refine / restructure 批（手动触发入队，执行体 application.wiki_ops）

    def submit_refine_batch(self, *, limit: int | None = None) -> list[Job]:
        """把 wiki 知识页逐页排队 refine——一页一个 job、一页一笔提交。

        与 sync 同一协议：入队即快照（当刻的页面清单），执行串行；
        同页幂等键收敛在途行，payload.batch 进 commit 尾注供整批撤销。
        """
        if self.wiki_dir is None:
            raise RuntimeError("submit_refine_batch 需要 wiki_dir")
        from wiki_agent.compiler.workflows.refine import refine_pages

        pages = refine_pages(self.wiki_dir)
        if limit is not None:
            pages = pages[:limit]
        batch = f"refine_{uuid4().hex}"
        jobs: list[Job] = []
        with self.store.database.transaction(immediate=True) as conn:
            for page in pages:
                resource = str(Path(page).resolve())
                jobs.append(
                    self.store.enqueue(
                        kind=Kind.REFINE,
                        resource=resource,
                        mode="manual",
                        payload={"batch": batch},
                        idempotency_key=f"refine:{resource}",
                        _conn=conn,
                    )
                )
        return jobs

    def submit_restructure(self, proposals: list[dict]) -> list[Job]:
        """已确认的提议切成执行单元排队：一个单元一个 job、一个单元一笔提交。

        与 sync 同一提交/执行分工：提议与确认发生在提交侧，job 只做执行。
        单元由 restructure.partition_units 划分（事务组 + 依赖闭包），
        按入参顺序入队、claim 按 (created_at, rowid) 保序执行。
        批间互斥：上一批 restructure 未到全终态时拒绝新提交
        （RestructureInProgress）——保证"撤销这一批"有清晰边界。
        resource 用带字面前缀的批内坐标，与绝对路径、issue id 构造性不相交；
        payload.batch 进每个单元 commit 的尾注，revert-batch 整批可撤。
        """
        from wiki_agent.compiler.restructure import Proposal, partition_units

        batch = f"restructure_{uuid4().hex}"
        typed = [Proposal(**item) for item in proposals]
        units = partition_units(typed)
        jobs: list[Job] = []
        with self.store.database.transaction(immediate=True) as conn:
            if self.store.in_flight_for_kinds((Kind.RESTRUCTURE,), _conn=conn) > 0:
                raise RestructureInProgress()
            for index, unit in enumerate(units):
                jobs.append(
                    self.store.enqueue(
                        kind=Kind.RESTRUCTURE,
                        resource=f"restructure:{batch}:{index}",
                        mode="manual",
                        payload={
                            "batch": batch,
                            "unit": index,
                            "proposals": [asdict(p) for p in unit],
                        },
                        idempotency_key=f"restructure:{batch}:{index}",
                        _conn=conn,
                    )
                )
        return jobs

    # 执行生命周期——Worker 独占

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        return self.store.claim_next(kinds=kinds)

    def mark_stage(self, job_id: str, stage: str) -> Job:
        # 同时刷新 updated_at——它是 recover_stale 的存活心跳
        return self.store.update(job_id, stage=stage)

    def complete_with_outcome(self, job: Job, result: JobResult) -> Job:
        """唯一终态提交点：jobs 行与 issue 联动同事务。

        终态写入带 CAS（仅 running 可翻转）：行已被取代/取消时迟到写静默
        跳过——不联动、不记账，返回行的现状。SyncState 与溯源档案页的写
        文件在提交后执行。手动重试模型下这里不产生任何后继 job：失败就是
        终态 + 一笔账。
        """
        with self.store.database.transaction(immediate=True) as conn:
            won = self.store.try_finalize(
                job.id,
                status=result.status,
                stage={"succeeded": "completed", "cancelled": "cancelled"}.get(result.status),
                error=(
                    str(result.detail.get("error") or "")[:500]
                    if result.status == "failed"
                    else None
                ),
                _conn=conn,
            )
            if not won:
                return self.store.get(job.id, _conn=conn)
            post_commit = self.outcomes.apply(job, result, conn)
        for action in post_commit:
            action()
        # 批的最后一个任务进入终态 → 本批快照目录完成使命（GC 规则一）
        batch = str(job.payload.get("batch") or "")
        if batch and self.store.count_in_flight_for_batch(batch) == 0:
            self.snapshots.drop_batch(batch)
        return self.store.get(job.id)

    def cancel_terminal(self, job: Job) -> None:
        """进程取消路径：终态 cancelled（CAS，不二次写）。

        取消不背失败也不还账——提交从未改变 issue 状态，无账可还，
        因此无需任何 issue/state 联动，一次条件写就是全部工作。
        """
        self.store.try_finalize(job.id, status="cancelled", stage="cancelled")

    # 读取

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.store.list(limit=limit)
