"""持久化任务的统一提交与生命周期接口。

Job 是唯一执行事实来源：全部提交入口在这里；终态写入只有一个点，
即 complete_with_outcome，jobs 行与 issue 联动同事务、带 CAS。

任务流水线互斥：写 wiki 的任一类任务（compile/delete/restructure/link，
见 WIKI_WRITE_KINDS）在途时，写类提交口不收新活，raise PipelineBusy；
同族自撞保留专门异常——sync 撞在途 compile/delete 报 SyncInProgress、
重组撞在途批报 RestructureInProgress（均为 PipelineBusy 子类）。retry
的幂等收敛排在闸之前：该 issue 已有在途挂账、或同一资源已被在途任务
占用时照常收敛返回既有 job，收敛不上且队列非空才拒。issue_action
（rescan）双向豁免——它不写 wiki、不产生快照批；代价如实：rescan 可
插在写批之间执行，结论可能基于改到一半的 wiki，批结束后再复扫即自愈。
判定只经由 JobStore.in_flight_kinds 从 jobs 表派生，无内存镜像。

同步基线闸门：restructure 的提议与 link 的判断基于 wiki 现状，wiki
落后于源材料时依据已过期。落后集合 = 脏源（scan_disk 与完成账之差，
与 sync_status 同源）− 隔离区（挂 open/blocked 编译失败账的源——失败
即保持脏是账本语义，不该卡死批操作），非空即 SyncBaselineLag 暂拒、
提示先 sync。SyncBaselineLag 与 PipelineBusy 分家：两种原因、两种补救
动作，消息不互相冒充。提交前的互斥检查在前（便宜），基线检查在后。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from wiki_agent.issues import IssueKind, IssueStatus, IssueStore
from wiki_agent.jobs import (
    WIKI_WRITE_KINDS,
    DuplicateInFlightJob,
    Job,
    JobResult,
    JobStore,
    Kind,
    PipelineBusy,
    RestructureInProgress,
    SyncBaselineLag,
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
        *,
        store: JobStore,
        issues: IssueStore,
        snapshots: SnapshotStore,
        outcomes: JobOutcomeHandler,
        sync_state: SyncState | None = None,
        materials_dir: str | Path | None = None,
        wiki_dir: str | Path | None = None,
    ):
        # 依赖全部由组合根注入；存储的唯一端口是 store——本类不认识 Database。
        self.store = store
        self.issues = issues
        self.snapshots = snapshots
        self.outcomes = outcomes
        # sync 快照对比需要完成账本
        self.sync_state = sync_state
        # 维护提交口要按盘面校验单元与计算批尾 link 波及面
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else None
        # 维护类任务（restructure/link）的基线判定要对比磁盘源材料与账本
        self.materials_dir = Path(materials_dir) if materials_dir is not None else None
        self.recovered_jobs = self.store.recover_stale()
        # 崩溃/中断遗留的无主快照目录在构造期清扫（保留名单=非终态任务引用的批）
        self.snapshots.sweep_orphans(self.store.in_flight_batch_ids())

    # 提交

    # 流水线互斥与基线判定：只在这几个 helper 里，提交口负责在正确位置调用

    def _in_flight_wiki_write_counts(
        self, _conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """写 wiki 各 kind 的在途行数——互斥判定的唯一依据。"""
        counts = self.store.in_flight_kinds(_conn=_conn)
        return {kind: n for kind, n in counts.items() if kind in WIKI_WRITE_KINDS}

    @staticmethod
    def _pipeline_busy(busy: dict[str, int]) -> PipelineBusy:
        parts = "、".join(f"{kind} {n} 个" for kind, n in sorted(busy.items()))
        return PipelineBusy(f"写 wiki 的任务在途（{parts}）：等当前批到达终态后再提交")

    @staticmethod
    def _sync_blocked(busy: dict[str, int]) -> PipelineBusy:
        """sync 提交被挡：撞同族的在途 compile/delete 保留 SyncInProgress 专门语义。"""
        if Kind.COMPILE in busy or Kind.DELETE in busy:
            return SyncInProgress()
        return JobService._pipeline_busy(busy)

    def sync_baseline_lag(self) -> set[str]:
        """基线落后集合 = 脏源 − 隔离区。

        脏判定与 sync_status 同源（scan_disk 对比完成账）；隔离区是挂着
        open/blocked 编译失败账的源——这些源已被打账隔离、页面与账本
        一致，不该再卡批操作。materials_dir 或 sync_state 未注入
        （离线装配）时返回空集，闸不适用。
        """
        if self.materials_dir is None or self.sync_state is None:
            return set()
        disk = scan_disk(self.materials_dir)
        dirty, _removed = self.sync_state.diff(disk)
        quarantined = {
            str(record.context.get("source_path") or "")
            for record in self.issues.list(
                statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
                kinds={IssueKind.INGESTION_FAILURE},
                limit=1000,
            )
        }
        return {str(Path(path).resolve()) for path, _digest in dirty} - quarantined

    def _raise_if_baseline_lagging(self) -> None:
        lagging = self.sync_baseline_lag()
        if not lagging:
            return
        preview = "、".join(sorted(lagging)[:3])
        raise SyncBaselineLag(
            f"{len(lagging)} 个源未同步（{preview}）：请先 sync，基线追平后再提交"
        )

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
        点击后文件再变，本次重试处理的仍是当时保存的这份副本。issue 终态由
        compile job 的 outcome 落，提交本身不改变 issue 状态。

        流水线互斥闸排在收敛之后：该 issue 已有在途挂账、或同一资源已被
        在途任务占用时照常收敛返回，只有两个收敛通道都空且写 wiki 任务
        在途时才 PipelineBusy 暂拒——双击 retry 的幂等语义不因加闸而破。
        """
        issue = self.issues.require(issue_id)
        source = resolve_retry_source(issue)
        resource = str(Path(source).resolve())
        busy = self._in_flight_wiki_write_counts()
        if busy and self.store.in_flight_job_by_issue(issue_id) is None:
            if self.store.in_flight_by_resource(resource) is None:
                raise self._pipeline_busy(busy)
        batch = f"retry_{uuid4().hex}"
        try:
            digest = self.snapshots.capture(batch, Path(resource).parent, [resource])[resource]
        except SnapshotError as exc:
            raise SourceUnavailableError(f"重试输入不可读: {exc}") from exc
        try:
            with self.store.transaction(immediate=True) as conn:
                existing = self.store.in_flight_job_by_issue(issue_id, _conn=conn)
                if existing is not None:
                    return existing
                if self.store.in_flight_by_resource(resource, _conn=conn) is None:
                    # 事务内复查：早退之后队列可能已被别的提交点亮
                    busy = self._in_flight_wiki_write_counts(_conn=conn)
                    if busy:
                        raise self._pipeline_busy(busy)
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
            "in_flight": self.store.in_flight_for_kinds(WIKI_WRITE_KINDS),
        }

    def submit_sync(self, source_dir: str | Path) -> list[Job]:
        """快照同步：点击时"磁盘 − 账本"之差即本批；脏文件复制保存后整批入队。

        语义契约——"快照是输入"：
        - 互斥串行：写 wiki 的四类任务任一在途即拒——撞在途的 compile/delete
          报 SyncInProgress（上一批没跑完不叠快照），撞 restructure/link
          报 PipelineBusy；无脏无删的空跑不入队任务、不受此限；
        - compile 任务的输入是点击时复制进 workspace/snapshots/<批>/
          的副本。执行期间原件修改、删除、复活都不影响本批；改动归下一次
          点击。payload.digest 就是副本的实际内容；
        - 失败不写账即保持脏，再次 sync 就是重试；脏文件若背着 open 失败账
          则同时挂账 issue_id，重试成功即解决；
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
            with self.store.transaction(immediate=True) as conn:
                self._close_vanished_failures(disk, conn)
            return []
        busy = self._in_flight_wiki_write_counts()
        if busy:
            raise self._sync_blocked(busy)
        batch = f"sync_{uuid4().hex}"
        try:
            digests = (
                self.snapshots.capture(batch, root, [path for path, _ in dirty]) if dirty else {}
            )
            jobs: list[Job] = []
            with self.store.transaction(immediate=True) as conn:
                busy = self._in_flight_wiki_write_counts(_conn=conn)
                if busy:
                    raise self._sync_blocked(busy)
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
        它不会再出现在任何任务里。按事实关闭并记录事件，不靠人工逐条清理。
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

    # 维护批（restructure 单元 + 批尾 link，执行体 application.wiki_ops）

    def submit_maintenance(self, units: list[dict]) -> list[Job]:
        """已确认的单元清单整批入队：一单元一 job，批尾自动跟波及面补链。

        单元声明在此完成最终校验（消解规则对当前盘面重跑一遍，违例即
        UnitError 拒绝——提交口不接受绕过消解的清单）；一个事务内先入队
        全部单元再入队 link 任务（范围 = 全部产出页 ∪ 消失页的入链页，
        提交时盘面静止故集合确定）。互斥与基线检查：重组自撞报
        RestructureInProgress，其他写在途报 PipelineBusy，未同步源报
        SyncBaselineLag。payload 只带声明——章节归属由执行时路由计算。
        """
        from wiki_agent.compiler.content_pages import all_content_slugs
        from wiki_agent.compiler.restructure import (
            Unit,
            UnitError,
            assert_units_valid,
            pages_linking_to,
        )

        if self.wiki_dir is None:
            raise RuntimeError("submit_maintenance 需要 wiki_dir")
        try:
            parsed = [Unit.from_dict(raw) for raw in units]
        except (TypeError, ValueError) as exc:
            raise UnitError(f"单元声明损坏: {exc}") from exc
        assert_units_valid(parsed, set(all_content_slugs(self.wiki_dir)))
        if not parsed:
            return []
        batch = f"restructure_{uuid4().hex}"
        jobs: list[Job] = []
        with self.store.transaction(immediate=True) as conn:
            self._raise_if_maintenance_blocked(conn)
            for index, unit in enumerate(parsed):
                jobs.append(
                    self.store.enqueue(
                        kind=Kind.RESTRUCTURE,
                        resource=f"restructure:{batch}:{index}",
                        mode="manual",
                        payload={"unit": unit.to_dict(), "batch": batch, "index": index},
                        idempotency_key=f"restructure:{batch}:{index}",
                        _conn=conn,
                    )
                )
            vanished = sorted({s for unit in parsed for s in unit.vanished})
            link_targets = sorted(
                {p for unit in parsed for p in unit.out_slugs} | set(pages_linking_to(self.wiki_dir, vanished))
            )
            for slug in link_targets:
                jobs.append(
                    self.store.enqueue(
                        kind=Kind.LINK,
                        resource=f"link:{slug}",
                        mode="tail",
                        payload={"slug": slug, "batch": batch},
                        idempotency_key=f"link:{batch}:{slug}",
                        _conn=conn,
                    )
                )
        return jobs

    def submit_link_batch(self, slugs: list[str] | None = None) -> list[Job]:
        """发现型补链：指定页（默认全库内容页）逐页入队，一页一 job 一提交。

        slug 按名册白名单校验（不存在即 ValueError，不产生注定空转的行）；
        互斥与基线检查同维护批。
        """
        from wiki_agent.compiler.content_pages import all_content_slugs

        if self.wiki_dir is None:
            raise RuntimeError("submit_link_batch 需要 wiki_dir")
        roster = all_content_slugs(self.wiki_dir)
        if slugs is None:
            targets = list(roster)
        else:
            unknown = [s for s in dict.fromkeys(slugs) if s not in set(roster)]
            if unknown:
                raise ValueError(f"不是可维护的 wiki 页: {unknown}")
            targets = list(dict.fromkeys(slugs))
        if not targets:
            return []
        batch = f"link_{uuid4().hex}"
        jobs: list[Job] = []
        with self.store.transaction(immediate=True) as conn:
            self._raise_if_maintenance_blocked(conn)
            for slug in targets:
                jobs.append(
                    self.store.enqueue(
                        kind=Kind.LINK,
                        resource=f"link:{slug}",
                        mode="manual",
                        payload={"slug": slug, "batch": batch},
                        idempotency_key=f"link:{batch}:{slug}",
                        _conn=conn,
                    )
                )
        return jobs

    def submit_maintenance_preview(self) -> Job:
        """整理结构分析入队（提议→复核→消解）：只产建议清单，不写 wiki。

        花钱前的闸与维护提交口同一判定：有写在途或基线落后即拒——
        基于动盘的提议没有执行价值。resource/幂等键固定，双击与重复
        发起收敛为同一在途分析。
        """
        with self.store.transaction(immediate=True) as conn:
            busy = self._in_flight_wiki_write_counts(_conn=conn)
            if busy:
                raise self._pipeline_busy(busy)
            self._raise_if_baseline_lagging()
            self._supersede_settled_previews(conn)
            return self.store.enqueue(
                kind=Kind.MAINTENANCE_PREVIEW,
                resource="maintenance:preview",
                mode="manual",
                payload={},
                idempotency_key="maintenance:preview",
                _conn=conn,
            )

    @staticmethod
    def _supersede_settled_previews(conn: sqlite3.Connection) -> None:
        """新分析发起即作废上一轮结果——不变量：未处置的终态分析行 ≤ 1。

        重跑分析等于宣告旧建议不再待办；处置记录仍随各行持久，
        界面读侧因此不需要任何"哪条算最新"的判断。
        """
        rows = conn.execute(
            "SELECT id, result_json FROM jobs"
            " WHERE kind = ? AND status IN ('succeeded', 'failed')",
            (Kind.MAINTENANCE_PREVIEW,),
        ).fetchall()
        for row in rows:
            result: dict[str, object] = json.loads(row["result_json"] or "{}")
            raw = result.get("preview")
            preview: dict[str, object] = dict(raw) if isinstance(raw, dict) else {}
            if "resolved_by" in preview:
                continue
            preview["resolved_by"] = "superseded"
            result["preview"] = preview
            conn.execute(
                "UPDATE jobs SET result_json = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), row["id"]),
            )

    def resolve_maintenance_preview(self, job_id: str, *, by: str) -> Job:
        """为一次分析结果记终态处置（dismissed 否决 / submitted 已入队执行）。

        处置过的建议行从队列消失——入队后重复打开会导致同一批建议
        二次入队；任务行保留做履历，处置方式记录在案。新一轮分析
        产出新结果，不受旧处置影响。非分析类任务拒绝。
        """
        if by not in ("dismissed", "submitted"):
            raise ValueError(f"未知的处置方式: {by}")
        job = self.store.get(job_id)
        if job.kind != Kind.MAINTENANCE_PREVIEW:
            raise ValueError(f"只能处置整理结构分析结果: {job.kind}")
        raw = job.result.get("preview")
        preview: dict[str, object] = dict(raw) if isinstance(raw, dict) else {}
        preview["resolved_by"] = by
        return self.store.set_result(job_id, {**job.result, "preview": preview})

    def _raise_if_maintenance_blocked(self, _conn: sqlite3.Connection | None = None) -> None:
        """维护类提交的共用闸：自撞专门异常优先，其余在途统一 PipelineBusy。"""
        busy = self._in_flight_wiki_write_counts(_conn=_conn)
        if Kind.RESTRUCTURE in busy:
            raise RestructureInProgress()
        if busy:
            raise self._pipeline_busy(busy)
        self._raise_if_baseline_lagging()

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
        with self.store.transaction(immediate=True) as conn:
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

        取消不联动任何账：提交时没改过 issue 状态，因此没有需要回转的
        记录，一次条件写就是全部工作。
        """
        self.store.try_finalize(job.id, status="cancelled", stage="cancelled")

    # 读取——适配器与脚本的读口，store 只在这里被调用

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.store.list(limit=limit)

    def get(self, job_id: str) -> Job:
        """按 id 读一行；不存在时抛 LookupError。"""
        return self.store.get(job_id)

    def count_in_flight(self) -> int:
        """在途（queued/running）行数——脚本驱动队列到空的判据。"""
        return self.store.count_in_flight()

    def in_flight_issue_ids(self) -> set[str]:
        """有挂账 queued/running job 的 issue id 集合。"""
        return set(self.store.open_issue_ids_with_in_flight_job())

    def wiki_write_in_flight(self) -> int:
        """写 wiki 的 job 在途行数——/wiki revert 的门的判据（WIKI_WRITE_KINDS）。"""
        return self.store.in_flight_for_kinds(WIKI_WRITE_KINDS)
