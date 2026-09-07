"""Content corrections live exclusively in the issue database."""

from __future__ import annotations

import asyncio
from pathlib import Path

from wiki_agent.command.commands import CommandContext, ResolveCommand
from wiki_agent.issues import IssueKind, IssueService, IssueStatus, IssueStore
from wiki_agent.issues.producers import report_correction
from wiki_agent.session import Session
from wiki_agent.tools import RecordCorrection, ToolRegistry


def test_record_correction_tool_reports_issue(tmp_path: Path):
    service = IssueService(IssueStore(tmp_path))
    tool = RecordCorrection(service)
    registry = ToolRegistry()
    registry.register(tool)

    output = asyncio.run(
        registry.execute(tool.name, {"page": "concepts/lambda.md", "issue": "示例代码缩进错误"})
    )

    assert "已记录" in output
    issues = service.store.list()
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.CONTENT_CORRECTION
    assert issues[0].resource["path"] == "concepts/lambda.md"
    assert not (tmp_path / "memory_store" / "corrections.jsonl").exists()


def test_record_correction_tool_rejects_empty_issue(tmp_path: Path):
    service = IssueService(IssueStore(tmp_path))
    tool = RecordCorrection(service)
    registry = ToolRegistry()
    registry.register(tool)

    output = asyncio.run(registry.execute(tool.name, {"issue": ""}))

    assert "未记录" in output
    assert service.store.list() == []


def test_resolve_command_flow(tmp_path: Path):
    class _Agent:
        issue_service = IssueService(IssueStore(tmp_path))

    report_correction(_Agent.issue_service, text="说法有误", page="concepts/a.md")
    report_correction(_Agent.issue_service, text="缺少示例", page="concepts/b.md")
    command = ResolveCommand()
    session = Session("test")

    listed = asyncio.run(
        command.execute(
            CommandContext(raw="/resolve", key="resolve", args="", session=session, agent=_Agent())
        )
    )
    assert "说法有误" in listed.text
    assert "缺少示例" in listed.text

    accepted = asyncio.run(
        command.execute(
            CommandContext(
                raw="/resolve accept 1",
                key="resolve",
                args="accept 1",
                session=session,
                agent=_Agent(),
            )
        )
    )
    assert "已确认待修" in accepted.text
    records = _Agent.issue_service.store.list()
    assert any(item.kind == IssueKind.QUALITY_ISSUE for item in records)
    assert any(
        item.kind == IssueKind.CONTENT_CORRECTION and item.status == IssueStatus.RESOLVED
        for item in records
    )


def test_resolve_invalid_index(tmp_path: Path):
    class _Agent:
        issue_service = IssueService(IssueStore(tmp_path))

    result = asyncio.run(
        ResolveCommand().execute(
            CommandContext(
                raw="/resolve reject 99",
                key="resolve",
                args="reject 99",
                session=Session("test"),
                agent=_Agent(),
            )
        )
    )
    assert "序号无效" in result.text
