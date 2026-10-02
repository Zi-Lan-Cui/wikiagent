"""持久化任务的统一提交口与生命周期入口。

提交入口集中于此；终态只由 complete_with_outcome 写入，jobs 行与 issue 变更
同事务、带 CAS。

互斥：写 wiki 的任务（compile/delete/restructure/link，见 WIKI_WRITE_KINDS）有
在途行时，写类提交抛 PipelineBusy；同族冲突用其子类 SyncInProgress（sync 遇在途
compile/delete）、RestructureInProgress（restructure 遇在途批）。issue retry 提交
先做幂等收敛——该 issue 已有在途行、或资源已被在途行占用时返回既有行，收敛不到
且队列非空才拒。issue_action（rescan）不受互斥约束、也不计入互斥：它不写 wiki、
不产生快照批，可穿插在写批之间执行，结论基于执行中的 wiki，写批结束后再 rescan
即更新。在途判定只由 JobStore.in_flight_kinds 从 jobs 表得出，无内存状态。

基线闸：restructure 与 link 基于 wiki 现状判断，wiki 落后源材料时结论过期。落后
集合 = 脏源（scan_disk 与完成记录之差）减去对应 open/blocked 编译失败的源（失败
源保持脏属正常，不因此阻塞批操作），非空则抛 SyncBaselineLag 提示先 sync。它与
PipelineBusy 各表一种原因与补救。提交口先查互斥、再查基线。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from wiki_agent.errors import ERROR_SUMMARY_LIMIT, summarize_error
from wiki_agent.issues import (
    IssueActionConflict,
    IssueKind,
    IssueRecord,
    IssueStatus,
    IssueStore,
)
from wiki_agent.jobs import (
    WIKI_WRITE_KINDS,
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

# detail 中的大字段：仅供 outcomes 同事务内消费（写完成记录、issue 记账、档案页
# 落盘），不持久化进 jobs 行——否则 state.db 随每轮 sync 膨胀、list() 反复反序列化大对象
_NON_PERSISTED_RESULT_KEYS = ("text", "source_page", "archive_ops", "raw")


def _persistable_result(detail: dict) -> dict:
    return {k: v for k, v in detail.items() if k not in _NON_PERSISTED_RESULT_KEYS}


class Baseline(Protocol):
    """jobs 提交口依赖的同步基线协议，由 sync 域实现（SyncBaseline）。

    经装配根注入、方法体不 import sync，保持 jobs 与 sync 的单向依赖。
    """

    def lagging_sources(self) -> set[str]: ...

    def inspect(
        self, source_dir: str | Path
    ) -> tuple[dict[str, str], list[tuple[str, str]], list[str]]: ...

    def recorded_hashes(self) -> set[str]: ...


@dataclass(frozen=True, slots=True)
class MaintenancePlan:
    """维护提交的入队素材：校验后的单元声明（已序列化）与批尾补链目标。

    units 顺序即入队顺序；link_slugs 为批尾补链页。
    """

    units: list[dict]
    link_slugs: list[str]


class MaintenancePlanner(Protocol):
    """维护批的领域规划面——实现住 application/compiler，按协议注入提交口。

    校验单元声明、算批尾补链波及面要读 wiki 盘面与 restructure 声明模型，
    是业务知识；jobs 提交口只保留事务、幂等键与互斥。方法体不 import
    compiler/application，装配根注入（与 Baseline 同构，断 jobs→compiler 边）。
    """

    def plan_maintenance(self, units: list[dict], wiki_dir: Path) -> MaintenancePlan:
        """消解规则对当前盘面重跑一遍，违例抛领域异常拒绝（UnitError 等）。"""
        ...

    def resolve_link_targets(self, slugs: list[str] | None, wiki_dir: Path) -> list[str]:
        """发现型补链目标：None=全库内容页；给定 slug 不在内容页列表抛 ValueError。"""
        ...


class JobService:
    """一切可执行工作的提交口与生命周期入口。"""

    def __init__(
        self,
        *,
        store: JobStore,
        issues: IssueStore,
        snapshots: SnapshotStore,
        outcomes: JobOutcomeHandler,
        baseline: Baseline | None = None,
        maintenance: MaintenancePlanner | None = None,
        wiki_dir: str | Path | None = None,
    ):
        # 依赖由组合根注入；存储只经 store 端口访问
        self.store = store
        self.issues = issues
        self.snapshots = snapshots
        self.outcomes = outcomes
        # 同步基线面由装配根注入；离线装配可为 None
        self.baseline = baseline
        # 维护规划面由装配根注入；离线/纯 sync 可为 None
        self.maintenance = maintenance
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else None
        self.recovered_jobs = 0
        # 清扫崩溃遗留的无引用快照目录，保留非终态任务引用的批
        self.snapshots.sweep_orphans(self.store.in_flight_batch_ids())

    def recover_stale(self) -> int:
        """把超时残留的 running 行回队——仅执行锁持有者调用。

        不在构造期自动跑：构造 service 不代表持有执行锁，第二进程构造期回队
        会与正在执行的 job 双写 wiki。装配根与脚本在持锁后显式调用。
        """
        self.recovered_jobs = self.store.recover_stale()
        return self.recovered_jobs

    # 互斥与基线判定的 helper

    def _in_flight_wiki_write_counts(
        self, _conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """写 wiki 各 kind 的在途行数，互斥判定的依据。"""
        counts = self.store.in_flight_kinds(_conn=_conn)
        return {kind: n for kind, n in counts.items() if kind in WIKI_WRITE_KINDS}

    @staticmethod
    def _pipeline_busy(busy: dict[str, int]) -> PipelineBusy:
        parts = "、".join(f"{kind} {n} 个" for kind, n in sorted(busy.items()))
        return PipelineBusy(f"写 wiki 的任务在途（{parts}）：等当前批到达终态后再提交")

    @staticmethod
    def _sync_blocked(busy: dict[str, int]) -> PipelineBusy:
        """sync 遇在途 compile/delete 抛 SyncInProgress，其余在途抛 PipelineBusy。"""
        if Kind.COMPILE in busy or Kind.DELETE in busy:
            return SyncInProgress()
        return JobService._pipeline_busy(busy)

    def sync_baseline_lag(self) -> set[str]:
        """基线落后集合；未注入基线面时返回空集。"""
        if self.baseline is None:
            return set()
        return self.baseline.lagging_sources()

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

    @staticmethod
    def _raise_if_retry_ineligible(issue: IssueRecord) -> None:
        """重试资格在提交口统一判定：编译失败记录且未终态。

        来源可读性在批内逐行判（resolve_retry_source）；标记 unavailable 的入账
        由调用方处理，提交口只拒绝、不改 issue 状态。
        """
        if issue.kind != IssueKind.INGESTION_FAILURE:
            raise IssueActionConflict(f"不是编译失败账，不能重试: {issue.kind}")
        if issue.status in {IssueStatus.RESOLVED, IssueStatus.DISMISSED}:
            raise IssueActionConflict("问题已终态——先重新打开再重试")

    def submit_issue_retry(self, issue_id: str) -> Job:
        """单发重试即一批——资格、收敛、互斥的语义见 submit_issue_retry_batch。"""
        return self.submit_issue_retry_batch([issue_id])[0]

    def submit_issue_retry_batch(self, issue_ids: list[str]) -> list[Job]:
        """批量重试：资格与输入快照在事务前，收敛与入队在单事务内。

        资格在提交口统一判定：编译失败记录、未终态、来源可读；来源不可读时
        标记 unavailable（在事务外，避免批事务持写锁时嵌套写库）。逐行做执行
        唯一性收敛：该 issue 已有在途行则返回既有行；资源被在途行占用（含本批
        前序行）则收敛到该行、其无关联 issue 时补挂。两个收敛都不到且批外有写
        wiki 在途时抛 PipelineBusy——在途计数在事务开始时取定，本批自产行不计入。

        快照语义同 submit_sync：digest 取自点击时复制的副本。任一行资格不过、
        输入不可读或遇互斥，整批回滚并删除已复制的快照目录。issue 终态由
        compile job 的 outcome 落，提交不改 issue 状态。
        """
        prepared: list[tuple[str, str, str, str]] = []
        fresh_batches: list[str] = []
        try:
            for issue_id in issue_ids:
                issue = self.issues.require(issue_id)
                self._raise_if_retry_ineligible(issue)
                unavailable = str(issue.retry.get("unavailable_reason") or "")
                if unavailable:
                    raise SourceUnavailableError(unavailable)
                try:
                    resource = str(Path(resolve_retry_source(issue)).resolve())
                    batch = f"retry_{uuid4().hex}"
                    fresh_batches.append(batch)
                    digest = self.snapshots.capture(
                        batch, Path(resource).parent, [resource]
                    )[resource]
                except (SourceUnavailableError, SnapshotError) as exc:
                    reason = str(exc)
                    self.mark_retry_unavailable(issue_id, reason)
                    raise SourceUnavailableError(reason) from exc
                prepared.append((issue_id, resource, batch, digest))
            jobs: list[Job] = []
            used: set[str] = set()
            with self.store.transaction(immediate=True) as conn:
                external_busy = self._in_flight_wiki_write_counts(_conn=conn)
                for issue_id, resource, batch, digest in prepared:
                    existing = self.store.in_flight_job_by_issue(issue_id, _conn=conn)
                    if existing is not None:
                        jobs.append(existing)
                        continue
                    occupant = self.store.in_flight_by_resource(resource, _conn=conn)
                    if occupant is not None:
                        if not occupant.issue_id:
                            occupant = self.store.attach_issue(
                                occupant.id, issue_id, _conn=conn
                            )
                        jobs.append(occupant)
                        continue
                    if external_busy:
                        raise self._pipeline_busy(external_busy)
                    used.add(batch)
                    jobs.append(
                        self.store.enqueue(
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
                    )
        except BaseException:
            for batch in fresh_batches:
                self.snapshots.drop_batch(batch)
            raise
        for batch in fresh_batches:
            if batch not in used:
                self.snapshots.drop_batch(batch)
        return jobs

    def mark_retry_unavailable(self, issue_id: str, reason: str) -> None:
        """标记该 issue 输入不可读：写 retry.unavailable_reason，OPEN 转 BLOCKED。"""
        record = self.issues.require(issue_id)
        self.issues.update_payloads(
            issue_id,
            retry={**record.retry, "unavailable_reason": reason},
            diagnostics={**record.diagnostics, "detail": reason},
            event="retry_source_unavailable",
        )
        if record.status == IssueStatus.OPEN:
            self.issues.transition(
                issue_id, IssueStatus.BLOCKED, event="blocked_source_unavailable"
            )

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
        """只读统计 dirty/removed 与在途数，不提交。"""
        if self.baseline is None:
            raise RuntimeError("sync_status 需要注入同步基线面")
        _disk, dirty, removed = self.baseline.inspect(source_dir)
        return {
            "dirty": len(dirty),
            "removed": len(removed),
            "in_flight": self.store.in_flight_for_kinds(WIKI_WRITE_KINDS),
        }

    def submit_sync(self, source_dir: str | Path) -> list[Job]:
        """快照同步：以点击时的磁盘与完成记录之差为本批，脏文件复制为快照后整批入队。

        - 互斥串行：写 wiki 的四类任务任一在途即拒——遇在途 compile/delete 抛
          SyncInProgress，遇 restructure/link 抛 PipelineBusy；无脏无删的空跑
          不入队、不受此限。
        - 输入是点击时复制到 workspace/snapshots/<batch>/ 的副本；执行期间原件的
          改动、删除、恢复都不影响本批，改动留到下一次 sync。payload.digest 取
          副本的实际内容。
        - 失败不写完成记录即保持脏，再次 sync 即重试；脏文件若有关联 open 失败
          记录，入队时挂 issue_id，重试成功后一并解决。
        - payload.batch 三个用途：快照目录名、wiki commit 批标识（按它定位整批
          撤销）、批内最后一个任务终态时删除目录的分组键。

        先复制后入队，保证任务存在则输入必在；入队失败或被拒时删除刚复制的目录，
        崩溃遗留由构造期清扫。
        """
        if self.baseline is None:
            raise RuntimeError("submit_sync 需要注入同步基线面")
        root = Path(source_dir).resolve()
        disk, dirty, removed = self.baseline.inspect(root)
        if not dirty and not removed:
            # 无任务时也要检查：源已消失的失败记录只依赖磁盘与完成记录判定
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
                    # delete 同 compile：来源有关联的活动失败记录就挂 issue_id
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
        """关闭源已不存在的活动失败记录：source 既不在磁盘也不在完成记录里，
        不会再出现在任何任务，据实关闭并记录事件。
        """
        assert self.baseline is not None
        hashed = self.baseline.recorded_hashes()
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

    # 维护批（restructure 单元 + 批尾 link）

    def submit_maintenance(self, units: list[dict]) -> list[Job]:
        """已确认的单元清单整批入队：一单元一 job，批尾自动跟受影响页补链。

        单元声明经 MaintenancePlanner 校验与影响范围计算；一个事务内先入队全部
        单元再入队 link 任务。互斥与基线检查：已有 restructure 在途抛
        RestructureInProgress，其他写在途抛 PipelineBusy，未同步源抛
        SyncBaselineLag。payload 只带声明，章节归属由执行时路由计算。
        """
        if self.wiki_dir is None:
            raise RuntimeError("submit_maintenance 需要 wiki_dir")
        if self.maintenance is None:
            raise RuntimeError("submit_maintenance 需要注入维护规划面")
        plan = self.maintenance.plan_maintenance(units, self.wiki_dir)
        if not plan.units:
            return []
        batch = f"restructure_{uuid4().hex}"
        jobs: list[Job] = []
        with self.store.transaction(immediate=True) as conn:
            self._raise_if_maintenance_blocked(conn)
            for index, unit in enumerate(plan.units):
                jobs.append(
                    self.store.enqueue(
                        kind=Kind.RESTRUCTURE,
                        resource=f"restructure:{batch}:{index}",
                        mode="manual",
                        payload={"unit": unit, "batch": batch, "index": index},
                        idempotency_key=f"restructure:{batch}:{index}",
                        _conn=conn,
                    )
                )
            for slug in plan.link_slugs:
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

        slug 经 MaintenancePlanner 按内容页白名单校验（不存在即 ValueError）；
        互斥与基线检查同维护批。
        """
        if self.wiki_dir is None:
            raise RuntimeError("submit_link_batch 需要 wiki_dir")
        if self.maintenance is None:
            raise RuntimeError("submit_link_batch 需要注入维护规划面")
        targets = self.maintenance.resolve_link_targets(slugs, self.wiki_dir)
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

        提交前互斥与基线判定同维护口：有写在途或基线落后即拒——基于执行中
        wiki 的提议没有执行价值。resource 与幂等键固定，重复发起收敛为同一在途分析。
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
        """发起新分析前，把上一轮未处置的终态分析标记为 superseded。

        不变量：未处置的终态分析行 ≤ 1。处置记录仍随各行持久，读侧无需判断
        哪条最新。
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
        """记录一次分析结果的终态处置（dismissed 否决 / submitted 已入队执行）。

        处置过的建议行不再出现在待办队列，避免重复入队；任务行保留处置记录。
        新一轮分析产新结果，不受旧处置影响。非分析类任务拒绝。
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
        """维护类提交的共用闸：已有 restructure 在途抛专门异常，其余在途抛 PipelineBusy。"""
        busy = self._in_flight_wiki_write_counts(_conn=_conn)
        if Kind.RESTRUCTURE in busy:
            raise RestructureInProgress()
        if busy:
            raise self._pipeline_busy(busy)
        self._raise_if_baseline_lagging()

    # 执行生命周期（Worker 独占）

    def claim_next(self, *, kinds: set[str] | None = None) -> Job | None:
        return self.store.claim_next(kinds=kinds)

    def mark_stage(self, job_id: str, stage: str) -> Job:
        # 刷新 updated_at：recover_stale 依据它判超时
        return self.store.update(job_id, stage=stage)

    def complete_with_outcome(self, job: Job, result: JobResult) -> Job:
        """唯一终态提交点：jobs 行与 issue 联动同事务。

        带 CAS（仅 running 可翻转）：行已被取代或取消时迟到写静默跳过——不联动、
        不改 issue，返回行的现状。SyncState 与溯源档案页的文件写在提交之后。失败
        即终态，不产生后继 job。
        """
        with self.store.transaction(immediate=True) as conn:
            won = self.store.try_finalize(
                job.id,
                status=result.status,
                stage={"succeeded": "done", "cancelled": "cancelled"}.get(result.status),
                error=(
                    summarize_error(result.detail.get("error"), ERROR_SUMMARY_LIMIT)
                    if result.status == "failed"
                    else None
                ),
                result=_persistable_result(result.detail),
                _conn=conn,
            )
            if not won:
                return self.store.get(job.id, _conn=conn)
            post_commit = self.outcomes.apply(job, result, conn)
        for action in post_commit:
            action()
        # 批内最后一个任务进终态后删除该批快照目录
        batch = str(job.payload.get("batch") or "")
        if batch and self.store.count_in_flight_for_batch(batch) == 0:
            self.snapshots.drop_batch(batch)
        return self.store.get(job.id)

    def cancel_terminal(self, job: Job) -> None:
        """进程取消路径：以 CAS 将行置为 cancelled。

        提交时未改 issue 状态，取消无需回转任何关联记录。
        """
        self.store.try_finalize(job.id, status="cancelled", stage="cancelled")

    # 读取（store 仅在此处被调用）

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.store.list(limit=limit)

    def get(self, job_id: str) -> Job:
        """按 id 读一行；不存在时抛 LookupError。"""
        return self.store.get(job_id)

    def count_in_flight(self) -> int:
        """在途（queued/running）行数——脚本驱动队列到空的判据。"""
        return self.store.count_in_flight()

    def in_flight_issue_ids(self) -> set[str]:
        """有关联 queued/running job 的 issue id 集合。"""
        return set(self.store.open_issue_ids_with_in_flight_job())

    def wiki_write_in_flight(self) -> int:
        """写 wiki 的在途 job 数，供 /wiki revert 前检查（WIKI_WRITE_KINDS）。"""
        return self.store.in_flight_for_kinds(WIKI_WRITE_KINDS)
