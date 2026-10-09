"""终态联动的装配注册：把各线提供的规则接进分发器。

分发器本体是机制（queue/outcomes），不知道任何业务线；这里按装配面
选择注册：issues 恒注册，sync 有 SyncState 才注册，maintenance 有提议
账本才注册。所有装配根（runtime、helpers、compile_batches）共用本工厂，
注册面差异只由注入的依赖决定。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.issues.outcomes import register_issue_outcomes
from wiki_agent.maintenance.outcomes import register_maintenance_outcomes
from wiki_agent.queue.outcomes import JobOutcomeHandler
from wiki_agent.sync.outcomes import register_sync_outcomes

if TYPE_CHECKING:
    from wiki_agent.compiler.extraction import ExtractionSourcePageWriter
    from wiki_agent.issues import IssueStore
    from wiki_agent.maintenance import ProposalStore
    from wiki_agent.sync.state import SyncState


def build_job_outcomes(
    *,
    issue_store: IssueStore,
    sync_state: SyncState | None = None,
    source_records_dir: str | Path | None = None,
    source_writer: ExtractionSourcePageWriter | None = None,
    proposals: ProposalStore | None = None,
) -> JobOutcomeHandler:
    """按注入装配规则；缺依赖的线不注册，其结算类别无联动。"""
    outcomes = JobOutcomeHandler()
    register_issue_outcomes(outcomes, issue_store)
    if sync_state is not None:
        register_sync_outcomes(
            outcomes,
            sync_state=sync_state,
            source_records_dir=source_records_dir,
            source_writer=source_writer,
        )
    if proposals is not None:
        register_maintenance_outcomes(outcomes, proposals)
    return outcomes
