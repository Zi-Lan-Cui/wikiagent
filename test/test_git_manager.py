"""WikiGitManager 的版本生命周期测试。"""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiki_agent.versioning import GitWorkspaceDirty, WikiGitManager


def test_tool_tasks_are_cancelled_and_joined():
    """Agent 取消时，显式创建的并发工具 Task 不留在后台。"""
    from wiki_agent.agent.react import ReActRunner
    from wiki_agent.conversation import ToolCall
    from wiki_agent.events import AgentHook, RunContext

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Registry:
        async def execute(self, name, params):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    class Hooks(AgentHook):
        pass

    agent = SimpleNamespace(
        tool_registry=Registry(),
        _hooks=Hooks(),
    )
    runner = ReActRunner(agent)
    tc = ToolCall(id="t1", name="slow", arguments={})

    async def scenario():
        task = asyncio.create_task(runner._execute_tools([tc], RunContext(session_key="s")))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert cancelled.is_set()
        assert not runner._active_tasks

    asyncio.run(scenario())


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    wiki = repo / "wiki"
    wiki.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Wiki Test"], cwd=repo, check=True)
    (wiki / "index.md").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "wiki"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    return repo, wiki


def test_initializes_a_standalone_repository_when_wiki_is_not_managed(tmp_path: Path):
    wiki = tmp_path / "notes"
    wiki.mkdir()
    (wiki / "index.md").write_text("# Notes\n", encoding="utf-8")

    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")

    assert manager.repo_root == wiki
    assert (wiki / ".git").is_dir()
    assert manager.status() == []
    assert (
        subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=wiki,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "wiki: initialize repository"
    )


def test_ignored_wiki_uses_a_nested_repository(tmp_path: Path):
    repo = tmp_path / "project"
    wiki = repo / "wiki"
    wiki.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("/wiki/\n", encoding="utf-8")
    (wiki / "index.md").write_text("# Private notes\n", encoding="utf-8")

    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")

    assert manager.repo_root == wiki
    assert (wiki / ".git").is_dir()
    assert manager.status() == []


def test_commit_and_rollback_create_revert_commit(tmp_path: Path):
    repo, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")
    run = manager.begin("r1", mode="refine")
    (wiki / "index.md").write_text("new\n", encoding="utf-8")
    (wiki / "concepts").mkdir()
    (wiki / "concepts" / "new.md").write_text("page\n", encoding="utf-8")
    scan_report = tmp_path / "runs" / "refine_r1" / "scan_report.md"
    scan_report.parent.mkdir(parents=True, exist_ok=True)
    scan_report.write_text("# scan\n", encoding="utf-8")
    committed = manager.commit(
        run,
        message="wiki: test",
        metadata={"scan_errors": 0},
        scan_report=scan_report,
    )
    assert committed.status == "committed"
    assert committed.commit != committed.before_commit
    assert (committed.run_dir / "run.json").exists()

    rollback_commit = manager.rollback(committed.commit, run_id=committed.run_id)
    assert rollback_commit
    assert (wiki / "index.md").read_text(encoding="utf-8") == "old\n"
    assert not (wiki / "concepts" / "new.md").exists()
    assert (repo / ".git" / "wiki-agent.lock").exists() is False


def test_abort_restores_tracked_and_removes_new_files(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")
    run = manager.begin("r2", mode="surgery")
    (wiki / "index.md").write_text("broken\n", encoding="utf-8")
    (wiki / "new.md").write_text("untracked page\n", encoding="utf-8")
    unicode_page = wiki / "sources" / "数据类型及色彩空间变换.md"
    unicode_page.parent.mkdir()
    unicode_page.write_text("untracked unicode page\n", encoding="utf-8")
    manager.abort(run, reason="scan error")
    assert (wiki / "index.md").read_text(encoding="utf-8") == "old\n"
    assert not (wiki / "new.md").exists()
    assert not unicode_page.exists()
    assert run.status == "aborted"


def test_change_summary_includes_untracked_and_deleted_files(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    (wiki / "removed.md").write_text("to remove\n", encoding="utf-8")
    subprocess.run(["git", "add", "wiki/removed.md"], cwd=wiki.parent, check=True)
    subprocess.run(["git", "commit", "-qm", "add removable"], cwd=wiki.parent, check=True)
    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")
    run = manager.begin("summary1", mode="compile")
    (wiki / "index.md").write_text("changed\n", encoding="utf-8")
    (wiki / "removed.md").unlink()
    (wiki / "new.md").write_text("new\n", encoding="utf-8")

    summary = manager.change_summary(run)
    assert summary["added"] == ["wiki/new.md"]
    assert summary["modified"] == ["wiki/index.md"]
    assert summary["deleted"] == ["wiki/removed.md"]
    manager.abort(run, reason="test cleanup")


def test_abort_stale_requires_dead_lock_and_restores_run(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")
    run = manager.begin("stale1", mode="refine")
    (wiki / "index.md").write_text("partial\n", encoding="utf-8")
    (wiki / "new.md").write_text("half-built\n", encoding="utf-8")
    manager._release_lock()
    lock = manager._lock_path
    lock.write_text("pid=9999999\nstarted_at=old\n", encoding="utf-8")

    aborted = manager.abort_stale(run.run_id)
    assert aborted.status == "aborted"
    assert (wiki / "index.md").read_text(encoding="utf-8") == "old\n"
    assert not (wiki / "new.md").exists()
    assert not lock.exists()


def test_abort_stale_rejects_live_lock(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")
    run = manager.begin("live1", mode="refine")
    try:
        with pytest.raises(Exception, match="仍由"):
            manager.abort_stale(run.run_id)
    finally:
        manager.abort(run, reason="test cleanup")


def test_begin_rejects_dirty_wiki_but_allows_unrelated_project_change(tmp_path: Path):
    repo, wiki = _repo(tmp_path)
    (repo / "src.txt").write_text("user change\n", encoding="utf-8")
    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")
    run = manager.begin("r3", mode="compile")
    manager.abort(run, reason="test")
    (wiki / "index.md").write_text("user wiki change\n", encoding="utf-8")
    with pytest.raises(GitWorkspaceDirty):
        manager.begin("r4", mode="compile")


def test_commit_handles_unicode_paths_without_status_mismatch(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki, run_root=tmp_path / "runs")
    run = manager.begin("unicode", mode="compile")
    page = wiki / "概念" / "中文页面.md"
    page.parent.mkdir()
    page.write_text("page\n", encoding="utf-8")
    scan_report = tmp_path / "runs" / "compile_unicode" / "scan_report.md"
    scan_report.parent.mkdir(parents=True, exist_ok=True)
    scan_report.write_text("# scan\n", encoding="utf-8")

    committed = manager.commit(
        run,
        message="wiki: unicode",
        scan_report=scan_report,
    )
    assert committed.status == "committed"
    assert "wiki/概念/中文页面.md" in committed.changed_files
