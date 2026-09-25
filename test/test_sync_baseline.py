"""同步基线闸门验收——refine/restructure 提交前，脏源（磁盘与完成账之差）
减去隔离区（挂 open/blocked 编译失败账的源）必须为空。

覆盖判定式两个方向：脏即拒（提交口兜底闸与 /refine 主闸）、隔离区豁免
（失败即保持脏是账本语义，不卡批操作——此条必须有测试钉住）；
以及 sync 入口不受基线闸约束（它是解药）。

直接运行:  .venv/bin/python -m pytest test/test_sync_baseline.py
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from helpers import make_job_service

from wiki_agent.agent.commands import CommandContext, RefineCommand
from wiki_agent.issues import IssueDraft, IssueKind, IssueStatus
from wiki_agent.jobs import Kind, SyncBaselineLag
from wiki_agent.jobs.service import JobService
from wiki_agent.sync.state import SyncState, digest_file_text

if TYPE_CHECKING:
    from wiki_agent.agent import ReActAgent
    from wiki_agent.conversation import Session

PAGES = {
    "op": "merge",
    "pages": ["concepts/a"],
    "target": "concepts/a",
    "reason": "t",
}


def _service(tmp: Path, *, materials: bool = True) -> JobService:
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True, exist_ok=True)
    (wiki / "concepts" / "a.md").write_text(
        '---\ntype: concept\ntitle: "A"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
        "---\n# A\n\n正文内容足够长以便通过页面识别。\n",
        encoding="utf-8",
    )
    materials_dir = tmp / "materials"
    if materials:
        materials_dir.mkdir(exist_ok=True)
    return make_job_service(
        tmp,
        wiki_dir=wiki,
        sync_state=SyncState(tmp / "watch" / "state.json"),
        materials_dir=materials_dir,
    )


def _make_clean(service: JobService, path: Path) -> None:
    """写文件并记入完成账——磁盘与账本一致即无落后。"""
    digest, text = digest_file_text(path)
    service.sync_state.record(str(path.resolve()), digest, text)


def _report_failure(service: JobService, source: Path, status: IssueStatus):
    issue = service.issues.report(
        IssueDraft(
            kind=IssueKind.INGESTION_FAILURE,
            title=f"{source.name} 处理失败",
            summary="plan 失败",
            resource={"type": "input_file", "path": source.name, "label": source.name},
            context={"source_path": str(source.resolve())},
        )
    )
    if status is IssueStatus.BLOCKED:
        service.issues.transition(issue.id, IssueStatus.BLOCKED, event="test_block")
    return issue


def test_clean_baseline_passes_refine(tmp_path: Path):
    service = _service(tmp_path)
    note = tmp_path / "materials" / "note.md"
    note.write_text("已同步的内容" * 10, encoding="utf-8")
    _make_clean(service, note)
    assert len(service.submit_refine_batch()) == 1


def test_clean_baseline_passes_restructure(tmp_path: Path):
    """restructure 单独验证：同一测试里先提 refine 会被互斥闸挡（那是另一道闸）。"""
    service = _service(tmp_path)
    note = tmp_path / "materials" / "note.md"
    note.write_text("已同步的内容" * 10, encoding="utf-8")
    _make_clean(service, note)
    assert len(service.submit_restructure([PAGES])) >= 1


def test_dirty_source_blocks_both_batch_entries(tmp_path: Path):
    service = _service(tmp_path)
    (tmp_path / "materials" / "new.md").write_text("未同步的新源" * 10, encoding="utf-8")
    with pytest.raises(SyncBaselineLag) as exc:
        service.submit_refine_batch()
    assert "1 个源未同步" in str(exc.value)
    with pytest.raises(SyncBaselineLag):
        service.submit_restructure([PAGES])
    # 互斥与基线是两道闸：队列空闲也不会放行脏基线
    assert service.count_in_flight() == 0


def test_quarantined_failure_does_not_block(tmp_path: Path):
    """隔离区豁免：失败源保持脏但已打账，不卡 refine/restructure。"""
    service = _service(tmp_path)
    bad = tmp_path / "materials" / "bad.md"
    bad.write_text("会失败的源" * 10, encoding="utf-8")
    _report_failure(service, bad, IssueStatus.OPEN)
    assert service.sync_baseline_lag() == set()
    assert len(service.submit_refine_batch()) == 1


def test_blocked_failure_also_quarantined(tmp_path: Path):
    service = _service(tmp_path)
    bad = tmp_path / "materials" / "bad.md"
    bad.write_text("会失败的源" * 10, encoding="utf-8")
    _report_failure(service, bad, IssueStatus.BLOCKED)
    assert service.sync_baseline_lag() == set()


def test_partially_dirty_still_blocks(tmp_path: Path):
    """一个干净一个脏：落后集合非空即拒，落后集合只含脏的。"""
    service = _service(tmp_path)
    clean = tmp_path / "materials" / "clean.md"
    clean.write_text("干净内容" * 10, encoding="utf-8")
    _make_clean(service, clean)
    (tmp_path / "materials" / "lag.md").write_text("落后的源" * 10, encoding="utf-8")
    assert service.sync_baseline_lag() == {str((tmp_path / "materials" / "lag.md").resolve())}


def test_sync_entry_is_never_baseline_gated(tmp_path: Path):
    """sync 是追平基线的手段，本身不设基线闸。"""
    service = _service(tmp_path)
    (tmp_path / "materials" / "new.md").write_text("未同步" * 10, encoding="utf-8")
    jobs = service.submit_sync(tmp_path / "materials")
    assert len(jobs) == 1 and jobs[0].kind == Kind.COMPILE


def test_offline_assembly_skips_gate(tmp_path: Path):
    """未注入 materials_dir 的离线装配：闸不适用，行为如旧。"""
    service = _service(tmp_path, materials=False)
    assert service.sync_baseline_lag() == set()
    assert len(service.submit_refine_batch()) == 1


def test_refine_command_main_gate_covers_dry_run(tmp_path: Path):
    """/refine 主闸在分析与入队之前——dry-run 预览同样被拒。"""
    service = _service(tmp_path)
    (tmp_path / "materials" / "new.md").write_text("未同步" * 10, encoding="utf-8")
    ctx = CommandContext(
        raw="/refine --dry-run",
        key="refine",
        args="--dry-run",
        session=cast("Session", SimpleNamespace()),  # 主闸之前不触会话
        agent=cast(  # 命令只触到 job_service 与 wiki 根，llm 轮不到出场
            "ReActAgent",
            SimpleNamespace(
                job_service=service,
                llm=None,
                tool_registry=SimpleNamespace(
                    get=lambda _name: SimpleNamespace(root=service.wiki_dir)
                ),
            ),
        ),
    )
    result = asyncio.run(RefineCommand().execute(ctx))
    assert result.text is not None and "已暂拒" in result.text and "new.md" in result.text
