"""任务流水线互斥验收——写 wiki 的四类任务任一在途时，其他写类提交口
一律 PipelineBusy 暂拒；同族自撞保留专门异常（sync→SyncInProgress、
restructure→RestructureInProgress）；retry 的幂等收敛排在闸之前；
issue_action（rescan）双向豁免。

在途形状用 store 直投造（不依赖被闸的入口），断言的是各提交口的
拒绝/放行语义，不涉及执行。

直接运行:  .venv/bin/python test/test_jobs_pipeline.py
"""

from pathlib import Path

import pytest
from helpers import make_job_service

from wiki_agent.issues import IssueDraft, IssueKind
from wiki_agent.jobs import (
    Kind,
    PipelineBusy,
    RestructureInProgress,
    SyncInProgress,
)
from wiki_agent.jobs.service import JobService
from wiki_agent.sync.state import SyncState


def _service(tmp: Path) -> JobService:
    wiki = tmp / "wiki"
    wiki.mkdir(exist_ok=True)
    return make_job_service(
        tmp,
        wiki_dir=wiki,
        sync_state=SyncState(tmp / "watch" / "state.json"),
    )


def _in_flight(service: JobService, kind: Kind, resource: str = "/abs/in-flight-probe") -> None:
    """直投一行在途任务——kind 是四种写类之一或 issue_action。"""
    service.submit(kind=kind, resource=resource, mode="test-fixture")


def _dirty_source(tmp: Path) -> Path:
    src = tmp / "materials"
    src.mkdir(exist_ok=True)
    (src / "note.md").write_text("脏材料" * 10, encoding="utf-8")
    return src


def _failure_issue(service: JobService, source: Path):
    return service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title=f"{source.name} 处理失败",
            summary="plan 失败",
            resource={"type": "input_file", "path": source.name, "label": source.name},
            context={"source_path": str(source)},
        )
    )


# —— sync 入口：同族与跨阶段的异常分流 ——


def test_sync_blocked_by_refine_raises_pipeline_busy(tmp_path: Path):
    service = _service(tmp_path)
    _dirty_source(tmp_path)
    _in_flight(service, Kind.REFINE)
    with pytest.raises(PipelineBusy) as exc:
        service.submit_sync(tmp_path / "materials")
    assert not isinstance(exc.value, SyncInProgress), "跨阶段报基类，不冒充 sync 专门语义"


def test_sync_blocked_by_restructure_raises_pipeline_busy(tmp_path: Path):
    service = _service(tmp_path)
    _dirty_source(tmp_path)
    _in_flight(service, Kind.RESTRUCTURE)
    with pytest.raises(PipelineBusy):
        service.submit_sync(tmp_path / "materials")


def test_sync_blocked_by_same_family_keeps_sync_in_progress(tmp_path: Path):
    service = _service(tmp_path)
    _dirty_source(tmp_path)
    _in_flight(service, Kind.COMPILE)
    with pytest.raises(SyncInProgress):
        service.submit_sync(tmp_path / "materials")


def test_empty_sync_passes_gate_while_refine_in_flight(tmp_path: Path):
    """无脏无删的空跑不入队任何任务——闸不适用，孤儿账清理照做。"""
    service = _service(tmp_path)
    (tmp_path / "materials").mkdir(exist_ok=True)
    _in_flight(service, Kind.REFINE)
    assert service.submit_sync(tmp_path / "materials") == []


# —— refine 入口：全家族互斥 + 自挡 ——


@pytest.mark.parametrize("busy_kind", [Kind.COMPILE, Kind.DELETE, Kind.REFINE])
def test_refine_blocked_by_any_write_kind(tmp_path: Path, busy_kind: Kind):
    service = _service(tmp_path)
    (tmp_path / "wiki" / "concepts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "concepts" / "x.md").write_text(
        '---\ntype: concept\ntitle: "X"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
        "---\n# X\n\n正文内容足够长以便通过页面识别。\n",
        encoding="utf-8",
    )
    _in_flight(service, busy_kind)
    with pytest.raises(PipelineBusy):
        service.submit_refine_batch()


# —— restructure 入口：批间自撞保留，跨阶段拒 ——


def test_restructure_blocked_by_refine_raises_pipeline_busy(tmp_path: Path):
    service = _service(tmp_path)
    _in_flight(service, Kind.REFINE)
    with pytest.raises(PipelineBusy):
        service.submit_restructure(
            [{"op": "merge", "pages": ["concepts/a"], "target": "concepts/a", "reason": "t"}]
        )


def test_restructure_batch_mutex_semantics_preserved(tmp_path: Path):
    service = _service(tmp_path)
    _in_flight(service, Kind.RESTRUCTURE)
    with pytest.raises(RestructureInProgress):
        service.submit_restructure(
            [{"op": "merge", "pages": ["concepts/a"], "target": "concepts/a", "reason": "t"}]
        )


# —— retry 入口：收敛排在闸前，收敛不上才被挡 ——


def test_retry_double_click_still_converges_while_own_job_in_flight(tmp_path: Path):
    """双击 retry：第一次挂的 compile 在途正是闸的判定对象——幂等收敛优先，不 409。"""
    service = _service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("重试内容" * 10, encoding="utf-8")
    issue = _failure_issue(service, source)
    first = service.submit_issue_retry(issue.id)
    second = service.submit_issue_retry(issue.id)
    assert second.id == first.id


def test_retry_blocked_by_unrelated_in_flight(tmp_path: Path):
    service = _service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("重试内容" * 10, encoding="utf-8")
    issue = _failure_issue(service, source)
    _in_flight(service, Kind.REFINE)
    with pytest.raises(PipelineBusy):
        service.submit_issue_retry(issue.id)


# —— rescan 双向豁免 ——


def test_rescan_in_flight_does_not_block_write_submits(tmp_path: Path):
    service = _service(tmp_path)
    _dirty_source(tmp_path)
    _in_flight(service, Kind.ISSUE_ACTION, resource="issue-probe")
    assert len(service.submit_sync(tmp_path / "materials")) >= 1


def test_write_in_flight_does_not_block_rescan(tmp_path: Path):
    service = _service(tmp_path)
    _in_flight(service, Kind.COMPILE)
    job = service.submit_issue_action("issue-probe", "rescan")
    assert job.kind == Kind.ISSUE_ACTION


# —— 误伤与形状守卫 ——


def test_all_entries_pass_when_queue_idle(tmp_path: Path):
    service = _service(tmp_path)
    src = _dirty_source(tmp_path)
    jobs = service.submit_sync(src)
    assert len(jobs) == 1
    for job in jobs:  # 让 sync 的在途行退场（update 直写终态，不走 running CAS）
        service.store.update(job.id, status="succeeded")
    service.submit_refine_batch()
    service.submit_restructure(
        [{"op": "merge", "pages": ["concepts/a"], "target": "concepts/a", "reason": "t"}]
    )


def test_busy_error_message_names_kinds_and_recovery(tmp_path: Path):
    service = _service(tmp_path)
    _dirty_source(tmp_path)
    _in_flight(service, Kind.REFINE)
    with pytest.raises(PipelineBusy) as exc:
        service.submit_sync(tmp_path / "materials")
    message = str(exc.value)
    assert "refine" in message and "终态" in message, "拒绝消息要可行动"


def test_legacy_errors_remain_pipeline_busy_subclasses():
    assert issubclass(SyncInProgress, PipelineBusy)
    assert issubclass(RestructureInProgress, PipelineBusy)


def test_sync_status_counts_all_write_kinds(tmp_path: Path):
    service = _service(tmp_path)
    (tmp_path / "materials").mkdir(exist_ok=True)
    _in_flight(service, Kind.REFINE)
    assert service.sync_status(tmp_path / "materials")["in_flight"] == 1
