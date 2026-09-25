"""测试组合根：按 Database → store → JobService 的次序装配。

代码各层不自建依赖；测试里等价于组合根的构造集中在这里。
"""

from pathlib import Path

from wiki_agent.issues import IssueService, IssueStore
from wiki_agent.jobs import JobStore
from wiki_agent.jobs.outcomes import JobOutcomeHandler
from wiki_agent.jobs.service import JobService
from wiki_agent.persistence import Database
from wiki_agent.snapshots import SnapshotStore
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
    wiki_dir: str | Path | None = None,
    sync_state: SyncState | None = None,
    source_records_dir: str | Path | None = None,
    outcomes: JobOutcomeHandler | None = None,
    materials_dir: str | Path | None = None,
) -> JobService:
    workspace = Path(workspace)
    database = Database(workspace)
    issues = IssueStore(database)
    return JobService(
        store=JobStore(database),
        issues=issues,
        snapshots=SnapshotStore(workspace),
        outcomes=outcomes
        or JobOutcomeHandler(
            issues,
            sync_state=sync_state,
            source_records_dir=source_records_dir,
        ),
        wiki_dir=wiki_dir,
        sync_state=sync_state,
        materials_dir=materials_dir,
    )
