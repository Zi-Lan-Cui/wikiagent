"""issues 线提交闸：重试的幂等收敛优先于互斥判定。

同一 issue 双击收敛到既有在途行；收敛不到且队列有别的写在途才拒。

直接运行:  .venv/bin/python -m pytest test/test_issues_gates.py
"""

from pathlib import Path

import pytest
from helpers import make_gate_service, put_in_flight

from wiki_agent.issues import IssueDraft, IssueKind
from wiki_agent.queue import Kind, PipelineBusy


def _failure_issue(service, source: Path):
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
    service = make_gate_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("重试内容" * 10, encoding="utf-8")
    issue = _failure_issue(service, source)
    first = service.submit_issue_retry(issue.id)
    second = service.submit_issue_retry(issue.id)
    assert second.id == first.id


def test_retry_blocked_by_unrelated_in_flight(tmp_path: Path):
    service = make_gate_service(tmp_path)
    source = tmp_path / "note.md"
    source.write_text("重试内容" * 10, encoding="utf-8")
    issue = _failure_issue(service, source)
    put_in_flight(service, Kind.LINK)
    with pytest.raises(PipelineBusy):
        service.submit_issue_retry(issue.id)
