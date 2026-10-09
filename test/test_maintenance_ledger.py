"""维护提议账本：状态机契约、提交口挂账与旧账迁移。"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import make_job_service

from wiki_agent.application.job_service import JobService
from wiki_agent.application.outcome_registry import build_job_outcomes
from wiki_agent.database import Database
from wiki_agent.issues import IssueStore
from wiki_agent.maintenance import (
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    ProposalStore,
    Resolution,
)
from wiki_agent.maintenance.restructure import Unit
from wiki_agent.queue import JobStore, Kind
from wiki_agent.queue.queue import JobQueue
from wiki_agent.snapshots import SnapshotStore

_FM = (
    '---\ntype: concept\ntitle: "{title}"\nsummary: "一个足够长的摘要信息"\n'
    'goal: "说明该页要解决的问题"\nrelated: []\nsources: []\n---\n'
    "# {title}\n\n## {title}主题\n\n内容足够长供维护使用。\n"
)

_PAYLOAD = {"healthy": False, "effective": [{"in_pages": ["concepts/a"], "out": []}]}


def _build_wiki(root: Path) -> Path:
    wiki = root / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    for slug, title in (("a", "甲"), ("b", "乙")):
        (wiki / "concepts" / f"{slug}.md").write_text(_FM.format(title=title), encoding="utf-8")
    return wiki


def test_record_supersedes_previous_pending(tmp_path: Path):
    """入账即作废旧 pending：待处置提议至多一条由库强制。"""
    store = ProposalStore(Database(tmp_path))
    first = store.record("job_1", _PAYLOAD)
    second = store.record("job_2", _PAYLOAD)
    assert [p.job_id for p in store.list_pending()] == ["job_2"]
    assert store.require(first.id).resolution is Resolution.SUPERSEDED
    assert store.require(second.id).resolution is Resolution.PENDING
    # 同一分析 job 重复入账拒绝
    with pytest.raises(ProposalAlreadyResolvedError):
        store.record("job_2", _PAYLOAD)


def test_dismiss_and_submit_cas(tmp_path: Path):
    """处置只从 pending 经 CAS 转入终态；重复处置与未知 id 各自报错。"""
    store = ProposalStore(Database(tmp_path))
    proposal = store.record("job_x", _PAYLOAD)
    dismissed = store.dismiss(proposal.id)
    assert dismissed.resolution is Resolution.DISMISSED
    with pytest.raises(ProposalAlreadyResolvedError):
        store.dismiss(proposal.id)
    with pytest.raises(ProposalNotFoundError):
        store.dismiss("prop_none")

    other = store.record("job_y", _PAYLOAD)
    submitted = store.mark_submitted(other.id, ["job_9", "job_10"])
    assert submitted.resolution is Resolution.SUBMITTED
    assert submitted.disposition["job_ids"] == ["job_9", "job_10"]
    with pytest.raises(ProposalAlreadyResolvedError):
        store.mark_submitted(other.id, ["job_11"])
    events = store.events(other.id)
    assert [e["event"] for e in events] == ["recorded", "submitted"]


def test_submit_restructure_marks_proposal_in_same_transaction(tmp_path: Path):
    """带 proposal_id 的提交：入队与账本处置同一事务，disposition 记全部任务行。"""
    wiki = _build_wiki(tmp_path)
    workspace = tmp_path / "workspace"
    service = make_job_service(workspace, wiki_dir=wiki)
    proposal = service.proposals.record("job_seed", _PAYLOAD)

    unit = Unit(in_pages=["concepts/a", "concepts/b"], out=[], reason="整体清理")
    jobs = service.submit_restructure([unit.to_dict()], proposal.id)

    assert jobs[0].kind == str(Kind.RESTRUCTURE)
    settled = service.proposals.require(proposal.id)
    assert settled.resolution is Resolution.SUBMITTED
    assert settled.disposition["job_ids"] == [job.id for job in jobs]
    assert service.proposals.list_pending() == []


def test_legacy_preview_rows_migrate_into_ledger(tmp_path: Path):
    """旧账（终态 job 行 result.preview.resolved_by）一次性转入提议表。"""
    workspace = tmp_path / "workspace"
    database = Database(workspace)
    issues = IssueStore(database)
    service = JobService(
        queue=JobQueue(
            store=JobStore(database),
            snapshots=SnapshotStore(workspace),
            outcomes=build_job_outcomes(issue_store=issues),
        ),
        issues=issues,
    )

    def settle_legacy(resolved_by: str) -> str:
        service.store.enqueue(
            kind=Kind.RESTRUCTURE_PREVIEW,
            resource="maintenance:preview",
            mode="manual",
            idempotency_key=f"legacy-{resolved_by or 'open'}",
        )
        # 终态 CAS 只对 running 生效：先领取再落终态，模拟完整执行
        claimed = service.claim_next(kinds={Kind.RESTRUCTURE_PREVIEW})
        assert claimed is not None
        preview = {"healthy": False, "effective": []}
        if resolved_by:
            preview["resolved_by"] = resolved_by
        service.store.try_finalize(
            claimed.id, status="succeeded", stage="done", result={"preview": preview}
        )
        return claimed.id

    done_job = settle_legacy("dismissed")
    old_open = settle_legacy("")
    latest_open = settle_legacy("")

    store = ProposalStore(database)
    rows = {p.job_id: p for p in store.list_all()}
    assert rows[done_job].resolution is Resolution.DISMISSED
    assert rows[latest_open].resolution is Resolution.PENDING
    assert rows[old_open].resolution is Resolution.SUPERSEDED
    # 迁移只发生一次：重复构造不新增行
    again = ProposalStore(database)
    assert [p.job_id for p in again.list_all()] == [p.job_id for p in store.list_all()]
