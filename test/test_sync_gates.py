"""sync 线提交闸与基线判定：异常家族分流、空跑豁免、落后公式与隔离区。

直投在途行覆盖判定内核——异常家族（SyncBaselineLag 与 PipelineBusy 分家）、
隔离区豁免、离线装配跳过。

直接运行:  .venv/bin/python -m pytest test/test_sync_gates.py
"""

from pathlib import Path

import pytest
from helpers import make_gate_service, make_job_service, put_in_flight

from wiki_agent.issues import IssueDraft, IssueKind
from wiki_agent.queue import Kind, PipelineBusy, SyncBaselineLag, SyncInProgress
from wiki_agent.snapshots import digest_file_text
from wiki_agent.sync.state import SyncState


def _dirty(tmp: Path, service, name: str = "new.md") -> Path:
    f = tmp / "materials" / name
    f.write_text("未同步的内容" * 10, encoding="utf-8")
    return f


def test_error_family_shape():
    assert issubclass(SyncInProgress, PipelineBusy)
    # 基线落后与队列在忙是两种原因，禁止再合进同一继承链
    assert not issubclass(SyncBaselineLag, PipelineBusy)


@pytest.mark.parametrize("busy_kind", [Kind.COMPILE, Kind.DELETE])
def test_sync_blocked_by_same_family_raises_sync_in_progress(tmp_path: Path, busy_kind: Kind):
    service = make_gate_service(tmp_path)
    _dirty(tmp_path, service)
    put_in_flight(service, busy_kind)
    with pytest.raises(SyncInProgress):
        service.submit_sync(tmp_path / "materials")


@pytest.mark.parametrize("busy_kind", [Kind.RESTRUCTURE, Kind.LINK])
def test_sync_blocked_by_maintenance_raises_pipeline_busy(tmp_path: Path, busy_kind: Kind):
    service = make_gate_service(tmp_path)
    _dirty(tmp_path, service)
    put_in_flight(service, busy_kind)
    with pytest.raises(PipelineBusy) as exc:
        service.submit_sync(tmp_path / "materials")
    assert not isinstance(exc.value, SyncInProgress)


def test_empty_sync_passes_while_anything_in_flight(tmp_path: Path):
    service = make_gate_service(tmp_path)
    put_in_flight(service, Kind.RESTRUCTURE)
    assert service.submit_sync(tmp_path / "materials") == []


def test_baseline_lag_formula_and_quarantine(tmp_path: Path):
    service = make_gate_service(tmp_path)
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
    service.baseline.raise_if_lagging()  # 不抛即通过
