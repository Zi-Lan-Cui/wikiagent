"""提交闸口的家族守卫：流水线互斥（sync/retry 两入口）与同步基线判定。

store 直投在途行覆盖判定内核——异常家族（SyncBaselineLag 与 PipelineBusy
分家）、收敛优先于闸、隔离区豁免、离线装配跳过。

直接运行:  .venv/bin/python -m pytest test/test_jobs_gates.py
"""

from pathlib import Path

import pytest
from helpers import make_job_service

from wiki_agent.issues import IssueDraft, IssueKind
from wiki_agent.jobs import (
    Kind,
    PipelineBusy,
    SyncBaselineLag,
    SyncInProgress,
)
from wiki_agent.jobs.service import JobService
from wiki_agent.snapshots import digest_file_text
from wiki_agent.sync.state import SyncState


def _service(tmp: Path) -> JobService:
    wiki = tmp / "wiki"
    wiki.mkdir(exist_ok=True)
    materials = tmp / "materials"
    materials.mkdir(exist_ok=True)
    return make_job_service(
        tmp,
        sync_state=SyncState(tmp / "watch" / "state.json"),
        materials_dir=materials,
    )


def _in_flight(service: JobService, kind: Kind) -> None:
    service.submit(kind=kind, resource=f"/probe/{kind.value}", mode="test-fixture")


def _dirty(tmp: Path, service: JobService, name: str = "new.md") -> Path:
    f = tmp / "materials" / name
    f.write_text("未同步的内容" * 10, encoding="utf-8")
    return f


# 异常家族形状


def test_error_family_shape():
    assert issubclass(SyncInProgress, PipelineBusy)
    # 基线落后与队列在忙是两种原因，禁止再合进同一继承链
    assert not issubclass(SyncBaselineLag, PipelineBusy)


# sync 入口：同族与跨阶段分流


@pytest.mark.parametrize("busy_kind", [Kind.COMPILE, Kind.DELETE])
def test_sync_blocked_by_same_family_raises_sync_in_progress(tmp_path: Path, busy_kind: Kind):
    service = _service(tmp_path)
    _dirty(tmp_path, service)
    _in_flight(service, busy_kind)
    with pytest.raises(SyncInProgress):
        service.submit_sync(tmp_path / "materials")


@pytest.mark.parametrize("busy_kind", [Kind.RESTRUCTURE, Kind.LINK])
def test_sync_blocked_by_maintenance_raises_pipeline_busy(tmp_path: Path, busy_kind: Kind):
    service = _service(tmp_path)
    _dirty(tmp_path, service)
    _in_flight(service, busy_kind)
    with pytest.raises(PipelineBusy) as exc:
        service.submit_sync(tmp_path / "materials")
    assert not isinstance(exc.value, SyncInProgress)


def test_empty_sync_passes_while_anything_in_flight(tmp_path: Path):
    service = _service(tmp_path)
    _in_flight(service, Kind.RESTRUCTURE)
    assert service.submit_sync(tmp_path / "materials") == []


# retry：收敛优先于闸


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


def test_retry_double_click_converges_while_own_job_in_flight(tmp_path: Path):
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
    _in_flight(service, Kind.LINK)
    with pytest.raises(PipelineBusy):
        service.submit_issue_retry(issue.id)


# 基线判定：公式与豁免


def test_baseline_lag_formula_and_quarantine(tmp_path: Path):
    service = _service(tmp_path)
    state = service.baseline.state  # 同一账本实例，避免双缓存分叉
    clean = _dirty(tmp_path, service, "clean.md")
    digest, text = digest_file_text(clean)
    state.record(str(clean.resolve()), digest, text)
    lag = _dirty(tmp_path, service, "lag.md")
    assert service.sync_baseline_lag() == {str(lag.resolve())}
    # 挂 open 失败账 → 隔离区豁免；blocked 同样豁免
    service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title="lag.md 处理失败",
            summary="失败",
            resource={"type": "input_file", "path": "lag.md", "label": "lag.md"},
            context={"source_path": str(lag.resolve())},
        )
    )
    assert service.sync_baseline_lag() == set()


def test_baseline_gate_skipped_without_materials(tmp_path: Path):
    service = make_job_service(tmp_path, sync_state=SyncState(tmp_path / "s.json"))
    assert service.sync_baseline_lag() == set()
    service._raise_if_baseline_lagging()  # 不抛即通过
