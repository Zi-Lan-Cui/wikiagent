"""Wiki 版本管理——"HEAD = 最近已结算状态"模型的 Git 原语层。

wiki 是机器管理的：人禁止直接改动生成页，工作区的未提交内容都出自执行。因此不存在需要保护的脏状态——任何时点 restore 到 HEAD 都是
安全操作，这也是没有锁、没有运行容器、没有 dirty 检查的原因：逐 job
协议（pre-reset → 执行 → 成功 commit / 失败 restore）保证每个 job 边界
收敛，崩溃留下的未提交改动由下一次 pre-reset 清除。

- 一切写 wiki 的 job 逐笔提交：compile `sync: <文件>`（retry 用
  `retry:`）、delete `sync: delete <文件>`、refine `refine: <页>`、
  restructure 一个执行单元一笔。body 携带 `Batch: <快照id>` 尾注。
  撤销一整批 = 按尾注在历史中选段 revert，纯历史操作，不回退账本。
- 运行留痕 = commit 历史本身；失败的未提交改动在 restore 前导出 patch 存档。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from wiki_agent.log import emit_event, get_logger
from wiki_agent.versioning.errors import GitCommitError, GitManagerError, GitScopeError

logger = get_logger("GIT_MANAGER")

_ADD_CHUNK = 50


class WikiGitManager:
    """Wiki scope 的 Git 原语：restore / commit / revert / history。"""

    def __init__(self, wiki_dir: str | Path):
        self.wiki_dir = Path(wiki_dir).resolve()
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = self._resolve_repository()
        try:
            self.scope = self.wiki_dir.relative_to(self.repo_root)
        except ValueError as exc:
            raise GitScopeError(f"Wiki 目录不在 Git 仓库内: {self.wiki_dir}") from exc

    def _resolve_repository(self) -> Path:
        """Reuse a repository that owns the Wiki, or initialize one locally.

        A parent repository does not own an ignored, entirely untracked Wiki.
        In that case a nested repository keeps private knowledge history
        independent from the application source repository.
        """
        discovered = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.wiki_dir,
            capture_output=True,
            text=True,
        )
        if discovered.returncode == 0:
            repo_root = Path(discovered.stdout.strip()).resolve()
            if repo_root == self.wiki_dir or self._parent_repository_owns_wiki(repo_root):
                return repo_root
        return self._initialize_wiki_repository()

    def _parent_repository_owns_wiki(self, repo_root: Path) -> bool:
        try:
            scope = self.wiki_dir.relative_to(repo_root)
        except ValueError:
            return False
        tracked = subprocess.run(
            ["git", "ls-files", "--", str(scope)],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if tracked.returncode == 0 and tracked.stdout.strip():
            return True
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(scope)],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        return ignored.returncode != 0

    def _initialize_wiki_repository(self) -> Path:
        initialized = subprocess.run(
            ["git", "init", "-q"],
            cwd=self.wiki_dir,
            capture_output=True,
            text=True,
        )
        if initialized.returncode:
            raise GitManagerError((initialized.stderr or initialized.stdout).strip())
        self._ensure_git_identity()
        for args in (
            ("add", "--all", "--", "."),
            ("commit", "--allow-empty", "-qm", "wiki: initialize repository"),
        ):
            result = subprocess.run(
                ["git", *args],
                cwd=self.wiki_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise GitManagerError((result.stderr or result.stdout).strip())
        emit_event("wiki_repository_initialized", wiki_dir=str(self.wiki_dir))
        return self.wiki_dir

    def _ensure_git_identity(self) -> None:
        defaults = {"user.name": "wiki-agent", "user.email": "wiki-agent@localhost"}
        for key, value in defaults.items():
            existing = subprocess.run(
                ["git", "config", "--get", key],
                cwd=self.wiki_dir,
                capture_output=True,
                text=True,
            )
            if existing.returncode == 0 and existing.stdout.strip():
                continue
            configured = subprocess.run(
                ["git", "config", key, value],
                cwd=self.wiki_dir,
                capture_output=True,
                text=True,
            )
            if configured.returncode:
                raise GitManagerError((configured.stderr or configured.stdout).strip())

    # Git 基础

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        # core.quotePath=false：diff/show 输出原始 UTF-8 路径，中文页面不被八进制转义污染
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
        )
        if check and result.returncode:
            raise GitManagerError((result.stderr or result.stdout).strip())
        return result

    def _scope_arg(self) -> str:
        return str(self.scope) or "."

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _status(self) -> list[str]:
        result = self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", self._scope_arg()
        )
        # -z 返回原始 UTF-8 路径，避免 Git 默认的 C 风格引号/八进制
        # 转义污染中文路径。rename/copy 的第二个路径是旧路径，状态
        # 展示和变更清单只保留最终路径。
        records = [item for item in result.stdout.split("\0") if item]
        lines: list[str] = []
        index = 0
        while index < len(records):
            record = records[index]
            status = record[:3]
            path = record[3:]
            lines.append(status + path)
            if "R" in status[:2] or "C" in status[:2]:
                index += 2
            else:
                index += 1
        return lines

    def _untracked(self) -> list[str]:
        return [
            path
            for path in self._git(
                "ls-files", "--others", "--exclude-standard", "-z", "--", self._scope_arg()
            ).stdout.split("\0")
            if path
        ]

    # 查询

    def head(self) -> str:
        """当前 HEAD commit。"""
        return self._head()

    def status(self) -> list[str]:
        """Wiki scope 内的未提交状态——协议下应恒为空，非空即执行遗留。"""
        return self._status()

    def is_clean(self) -> bool:
        return not self._status()

    def history(self, limit: int = 20) -> list[str]:
        """返回 Wiki scope 的 Git 历史。"""
        return self._git(
            "log",
            f"-{max(1, limit)}",
            "--date=short",
            "--pretty=format:%h %ad %s",
            "--",
            self._scope_arg(),
        ).stdout.splitlines()

    def diff_commit(self, commit: str) -> str:
        """某个已提交版本的完整 diff。"""
        return self._git("show", "--format=", commit, "--", self._scope_arg()).stdout

    def change_summary(self, since: str) -> dict[str, list[str]]:
        """相对基线 commit 的 Wiki 变化归类。

        ``git diff`` 不包含未跟踪的新文件，因此同时读取 porcelain 状态；
        这样 compile 报告不会漏掉刚生成、尚未纳入 Git 的页面。
        """
        result = self._git(
            "diff", "--name-status", "-z", since, "--", self._scope_arg()
        )
        summary = {"added": [], "modified": [], "deleted": [], "renamed": []}
        seen: set[str] = set()
        records = [item for item in result.stdout.split("\0") if item]
        index = 0
        while index < len(records):
            code = records[index]
            if code.startswith(("R", "C")) and index + 2 < len(records):
                path = records[index + 2]
                index += 3
            elif index + 1 < len(records):
                path = records[index + 1]
                index += 2
            else:
                break
            if path in seen:
                continue
            seen.add(path)
            if code.startswith("A"):
                bucket = "added"
            elif code.startswith("D"):
                bucket = "deleted"
            elif code.startswith("R"):
                bucket = "renamed"
            else:
                bucket = "modified"
            summary[bucket].append(path)
        for line in self._status():
            if not line.startswith("?? "):
                continue
            path = line[3:].strip()
            if path not in seen:
                summary["added"].append(path)
                seen.add(path)
        for paths in summary.values():
            paths.sort()
        return summary

    # 结算原语

    def restore(self) -> None:
        """工作区恢复到 HEAD：还原跟踪文件、删除 scope 内未跟踪文件。

        pre-reset 与失败撤销共用。只删文件不调用无范围 git clean；
        NUL 分隔避免中文路径被 core.quotePath 转义后无法定位。
        """
        had_changes = bool(self._status())
        if had_changes:
            # 空仓库/全部内容未跟踪场景：scope 内没有跟踪文件时 restore 会因
            # pathspec 不匹配报错——此时只有未跟踪内容可清
            if self._git("ls-files", "--", self._scope_arg()).stdout.strip():
                self._git("restore", "--staged", "--worktree", "--", self._scope_arg())
            for path in self._untracked():
                target = (self.repo_root / path).resolve()
                if target.is_file() and target.is_relative_to(self.wiki_dir):
                    target.unlink()
            self._prune_empty_dirs()
            emit_event("wiki_restored_to_head", wiki_dir=str(self.wiki_dir))

    def _prune_empty_dirs(self) -> None:
        """删除未提交改动留下的空目录（Git 不跟踪目录，restore 不会清理它们）。"""
        for directory in sorted(
            (p for p in self.wiki_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
        ):
            if ".git" in directory.parts:
                continue
            try:
                directory.rmdir()
            except OSError:
                pass  # 非空目录自然失败

    def working_patch(self) -> str:
        """未提交改动的完整 diff（restore 前存档用；新文件以 intent-to-add 纳入）。

        add -N 只改 index、restore 收尾时统一撤掉，不改工作区内容。
        """
        untracked = self._untracked()
        for i in range(0, len(untracked), _ADD_CHUNK):
            self._git("add", "--intent-to-add", "--", *untracked[i : i + _ADD_CHUNK])
        diff = self._git("diff", "--", self._scope_arg()).stdout
        if untracked:
            self._git("reset", "-q", "--", self._scope_arg())
        return diff

    def commit_all(self, subject: str, *, body: str = "") -> str | None:
        """提交 Wiki scope 的全部变更；无变更返回 None（noop 成功不产生 commit）。

        pathspec 形式的 commit 只覆盖 scope 路径——仓库中其他位置即使
        有暂存内容也不会被带入。
        """
        self._git("add", "-A", "--", self._scope_arg())
        # --quiet: 返回码 0=无变更、1=有变更
        probe = self._git("diff", "--cached", "--quiet", "--", self._scope_arg(), check=False)
        if probe.returncode == 0:
            return None
        args = ["commit", "-qm", subject]
        if body:
            args += ["-m", body]
        result = self._git(*args, "--", self._scope_arg(), check=False)
        if result.returncode:
            self._git("reset", "-q", "--", self._scope_arg(), check=False)
            raise GitCommitError((result.stderr or result.stdout).strip())
        commit = self._head()
        emit_event("wiki_committed", commit=commit[:8], subject=subject)
        return commit

    # 历史回撤

    def batch_commits(self, batch_id: str) -> list[str]:
        """一次快照批的全部 commit（新→旧）。"""
        return [
            line
            for line in self._git(
                "log",
                "--format=%H",
                "--fixed-strings",
                f"--grep=Batch: {batch_id}",
                "--",
                self._scope_arg(),
            ).stdout.splitlines()
            if line
        ]

    def revert_commit(self, commit: str) -> str:
        """回撤单个已提交版本，生成反向提交。"""
        return self._revert([commit], f"revert: {commit[:8]}")

    def revert_batch(self, batch_id: str) -> str:
        """撤销一整批：revert 该批全部 commit（新→旧），生成一笔反向提交。

        纯历史操作——sync 完成账不随之回退（账本记的是"当时确实编译过"），
        回撤后要让内容重新进 wiki 就再点一次 sync。
        """
        commits = self.batch_commits(batch_id)
        if not commits:
            raise GitManagerError(f"找不到批次 {batch_id} 的提交记录")
        return self._revert(commits, f"revert: batch {batch_id}")

    def _revert(self, commits: list[str], subject: str) -> str:
        # 先收敛到 HEAD——revert 要求干净工作区，而这里的"脏"只可能出自执行
        self.restore()
        result = self._git("revert", "-n", *commits, check=False)
        if result.returncode:
            self._git("revert", "--abort", check=False)
            raise GitManagerError((result.stderr or result.stdout).strip())
        probe = self._git("diff", "--cached", "--quiet", check=False)
        if probe.returncode == 0:
            self._git("reset", "-q", check=False)
            raise GitManagerError("revert 没有产生任何变更")
        result = self._git("commit", "-qm", subject, check=False)
        if result.returncode:
            self._git("revert", "--abort", check=False)
            raise GitManagerError((result.stderr or result.stdout).strip())
        commit = self._head()
        emit_event("wiki_reverted", commit=commit[:8], subject=subject, reverted=commits[:1])
        return commit
