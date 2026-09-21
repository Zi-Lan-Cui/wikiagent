"""WikiGitManager Git 原语层测试——restore/commit/revert 与批尾注。"""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from wiki_agent.versioning import WikiGitManager


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
        hooks=Hooks(),
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

    manager = WikiGitManager(wiki)

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

    manager = WikiGitManager(wiki)

    assert manager.repo_root == wiki
    assert (wiki / ".git").is_dir()
    assert manager.status() == []


def test_commit_all_then_revert_restores_content(tmp_path: Path):
    repo, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki)
    assert (repo / ".git" / "wiki-agent.lock").exists() is False  # 锁已随 run 容器退役
    (wiki / "index.md").write_text("new\n", encoding="utf-8")
    (wiki / "concepts").mkdir()
    (wiki / "concepts" / "new.md").write_text("page\n", encoding="utf-8")

    commit = manager.commit_all("sync: note.md")
    assert commit
    assert manager.is_clean()

    rollback = manager.revert_commit(commit)
    assert rollback != commit
    assert (wiki / "index.md").read_text(encoding="utf-8") == "old\n"
    assert not (wiki / "concepts" / "new.md").exists()


def test_commit_all_returns_none_without_changes(tmp_path: Path):
    """noop 成功（plan 空/内容未变）不造空提交。"""
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki)
    assert manager.commit_all("sync: unchanged.md") is None


def test_restore_reverts_tracked_and_removes_untracked_debris(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki)
    (wiki / "index.md").write_text("broken\n", encoding="utf-8")
    (wiki / "new.md").write_text("untracked page\n", encoding="utf-8")
    unicode_page = wiki / "sources" / "数据类型及色彩空间变换.md"
    unicode_page.parent.mkdir()
    unicode_page.write_text("untracked unicode page\n", encoding="utf-8")

    manager.restore()

    assert (wiki / "index.md").read_text(encoding="utf-8") == "old\n"
    assert not (wiki / "new.md").exists()
    assert not unicode_page.exists()
    assert not (wiki / "sources").exists()  # 残骸空目录一并清理
    assert manager.is_clean()


def test_restore_tolerates_dirty_start_no_lock_no_check(tmp_path: Path):
    """人禁止改 wiki：未提交内容=残骸，restore 无条件收敛，不存在 dirty 拒绝。"""
    _, wiki = _repo(tmp_path)
    (wiki / "index.md").write_text("user wiki change\n", encoding="utf-8")
    manager = WikiGitManager(wiki)
    manager.restore()  # 不抛
    assert (wiki / "index.md").read_text(encoding="utf-8") == "old\n"


def test_change_summary_includes_untracked_and_deleted_files(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    (wiki / "removed.md").write_text("to remove\n", encoding="utf-8")
    subprocess.run(["git", "add", "wiki/removed.md"], cwd=wiki.parent, check=True)
    subprocess.run(["git", "commit", "-qm", "add removable"], cwd=wiki.parent, check=True)
    manager = WikiGitManager(wiki)
    since = manager.head()
    (wiki / "index.md").write_text("changed\n", encoding="utf-8")
    (wiki / "removed.md").unlink()
    (wiki / "new.md").write_text("new\n", encoding="utf-8")

    summary = manager.change_summary(since)
    assert summary["added"] == ["wiki/new.md"]
    assert summary["modified"] == ["wiki/index.md"]
    assert summary["deleted"] == ["wiki/removed.md"]
    manager.restore()


def test_working_patch_captures_debris_without_moving_state(tmp_path: Path):
    """失败残骸快照：新文件内容要进 patch（intent-to-add），且拍完仍脏。"""
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki)
    (wiki / "concepts").mkdir()
    (wiki / "concepts" / "partial.md").write_text("half-written\n", encoding="utf-8")
    (wiki / "index.md").write_text("edited\n", encoding="utf-8")

    patch = manager.working_patch()

    assert "half-written" in patch
    assert "edited" in patch
    assert not manager.is_clean()  # 只借 index 拍快照，不结算
    manager.restore()
    assert manager.is_clean()


def test_commit_handles_unicode_paths(tmp_path: Path):
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki)
    page = wiki / "概念" / "中文页面.md"
    page.parent.mkdir()
    page.write_text("page\n", encoding="utf-8")

    commit = manager.commit_all("sync: 中文.md")
    assert commit
    assert "wiki/概念/中文页面.md" in manager.diff_commit(commit)


def test_batch_trailer_selects_commits_and_revert_batch_undoes_all(tmp_path: Path):
    """撤销一批 = 按 `Batch:` 尾注选段 revert——纯历史操作。"""
    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki)
    (wiki / "a.md").write_text("A\n", encoding="utf-8")
    manager.commit_all("sync: a.md", body="Batch: sync_batch1")
    (wiki / "b.md").write_text("B\n", encoding="utf-8")
    manager.commit_all("sync: b.md", body="Batch: sync_batch1")
    # 之后的另一批不被波及
    (wiki / "c.md").write_text("C\n", encoding="utf-8")
    later = manager.commit_all("sync: c.md", body="Batch: sync_batch2")
    assert later

    commits = manager.batch_commits("sync_batch1")
    assert len(commits) == 2

    rollback = manager.revert_batch("sync_batch1")
    assert rollback != later
    assert not (wiki / "a.md").exists()
    assert not (wiki / "b.md").exists()
    assert (wiki / "c.md").exists()
    assert manager.is_clean()


def test_revert_batch_rejects_unknown_batch(tmp_path: Path):
    import pytest

    from wiki_agent.versioning import GitManagerError

    _, wiki = _repo(tmp_path)
    manager = WikiGitManager(wiki)
    with pytest.raises(GitManagerError, match="找不到批次"):
        manager.revert_batch("sync_missing")
