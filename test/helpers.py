"""测试装配根：按 Database → store → JobQueue → 线 coordinator → 壳 的次序装配。

代码各层不自建依赖；测试里等价于装配根的构造集中在这里。
"""

from pathlib import Path

from wiki_agent.application.outcome_registry import build_job_outcomes
from wiki_agent.compiler.extraction import ExtractionSourcePageWriter
from wiki_agent.database import Database
from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.maintenance import ProposalStore
from wiki_agent.maintenance.coordinator import MaintenanceCoordinator
from wiki_agent.queue import JobStore
from wiki_agent.queue.outcomes import JobOutcomeHandler
from wiki_agent.queue.queue import JobQueue
from wiki_agent.queue.service import JobService
from wiki_agent.snapshots import SnapshotStore
from wiki_agent.sync.baseline import SyncBaseline
from wiki_agent.sync.coordinator import SyncCoordinator
from wiki_agent.sync.state import SyncState


def make_job_store(workspace: str | Path) -> JobStore:
    return JobStore(Database(workspace))


def make_issue_store(workspace: str | Path) -> IssueStore:
    return IssueStore(Database(workspace))


def make_issue_service(workspace: str | Path) -> IssueService:
    return IssueService(make_issue_store(workspace))


def make_job_service(
    workspace: str | Path,
    *,
    sync_state: SyncState | None = None,
    source_records_dir: str | Path | None = None,
    outcomes: JobOutcomeHandler | None = None,
    materials_dir: str | Path | None = None,
    wiki_dir: str | Path | None = None,
) -> JobService:
    workspace = Path(workspace)
    database = Database(workspace)
    issues = IssueStore(database)
    proposals = ProposalStore(database)
    snapshots = SnapshotStore(workspace)
    queue = JobQueue(
        store=JobStore(database),
        snapshots=snapshots,
        outcomes=outcomes
        or build_job_outcomes(
            issue_store=issues,
            sync_state=sync_state,
            source_records_dir=source_records_dir,
            source_writer=ExtractionSourcePageWriter(),
            proposals=proposals,
        ),
    )
    # sync_state 给了就接基线面与 sync 线；materials_dir 仅 lagging 判定用，缺省占位
    baseline = (
        SyncBaseline(state=sync_state, issues=issues, materials_dir=materials_dir or workspace)
        if sync_state is not None
        else None
    )
    return JobService(
        queue=queue,
        issues=issues,
        sync=(
            SyncCoordinator(
                queue=queue, issues=issues, snapshots=snapshots, baseline=baseline
            )
            if baseline is not None
            else None
        ),
        # 维护线提交入口常驻装配：闸与校验在 coordinator 内，wiki_dir 缺用时提交口自拒
        maintenance=MaintenanceCoordinator(
            queue=queue, baseline=baseline, wiki_dir=wiki_dir, proposals=proposals
        ),
        baseline=baseline,
        proposals=proposals,
    )
