"""Wiki Git 版本管理——存档、提交、失败恢复和历史回撤的唯一入口。

业务层只负责写 Wiki 并执行 scan；GitManager 负责：

    begin → commit / abort → rollback

设计约束：
- 默认要求 scope 工作区干净，避免覆盖用户未提交修改。
- 所有 Git 操作都限制在 scope 内；不使用无范围 reset/clean。
- 当前运行失败恢复到 before_commit；已提交版本使用 git revert。
- run.json/diff.patch 是审计存档，不混入 Wiki commit。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from wiki_agent.log import emit_event, get_logger
from wiki_agent.versioning.errors import (
    GitCommitError,
    GitManagerError,
    GitScopeError,
    GitWorkspaceDirty,
)
from wiki_agent.versioning.models import GitRun

logger = get_logger("GIT_MANAGER")


class WikiGitManager:
    """管理 Wiki 写入运行的 Git 生命周期。"""

    def __init__(
        self,
        wiki_dir: str | Path,
        *,
        run_root: str | Path | None = None,
        require_clean: bool = True,
    ):
        self.wiki_dir = Path(wiki_dir).resolve()
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = self._resolve_repository()
        try:
            self.scope = self.wiki_dir.relative_to(self.repo_root)
        except ValueError as exc:
            raise GitScopeError(f"Wiki 目录不在 Git 仓库内: {self.wiki_dir}") from exc
        self.run_root = (
            Path(run_root).resolve() if run_root else self.wiki_dir.parent / "workspace" / "runs"
        )
        self.require_clean = require_clean
        self._lock_path = self.repo_root / ".git" / "wiki-agent.lock"
        self._lock_owned = False

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

    # Git 基础操作

    def _git_path(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.wiki_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise GitManagerError((result.stderr or result.stdout).strip())
        return result.stdout.strip()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
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

    def status(self) -> list[str]:
        """返回 Wiki scope 内的未提交状态。"""
        return self._status()

    def change_summary(self, run: GitRun) -> dict[str, list[str]]:
        """按 Git 状态归类本次运行相对基线的文件变化。

        ``git diff`` 不包含未跟踪的新文件，因此同时读取 porcelain 状态；
        这样 compile 报告不会漏掉刚生成、尚未纳入 Git 的页面。
        """
        result = self._git(
            "diff", "--name-status", "-z", run.before_commit, "--", self._scope_arg()
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

    def _acquire_lock(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\nstarted_at={datetime.now(UTC).isoformat()}\n")
            self._lock_owned = True
        except FileExistsError as exc:
            raise GitManagerError(f"Wiki Git 正在被其他运行占用: {self._lock_path}") from exc

    def stale_status(self) -> dict:
        """检查锁和 active run，绝不自动清理。"""
        lock = None
        if self._lock_path.exists():
            values = {}
            for line in self._lock_path.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            pid = int(values.get("pid", "0") or 0)
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            lock = {**values, "alive": alive, "stale": not alive}

        active_runs = []
        if self.run_root.is_dir():
            for run_file in self.run_root.glob("*/run.json"):
                try:
                    payload = json.loads(run_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if payload.get("status") == "active":
                    active_runs.append(payload)
        return {"lock": lock, "active_runs": active_runs}

    def clear_stale_lock(self) -> bool:
        """人工清理已确认进程不存在的锁；不会回撤 Wiki 文件。"""
        status = self.stale_status()
        lock = status.get("lock")
        if not lock or not lock.get("stale"):
            return False
        self._lock_path.unlink(missing_ok=True)
        emit_event("git_stale_lock_cleared", lock_path=str(self._lock_path))
        return True

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

    def run_record(self, run_id: str) -> dict | None:
        """按 run_id 读取审计记录。"""
        for run_file in self.run_root.glob("*/run.json"):
            try:
                payload = json.loads(run_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("run_id") == run_id or run_file.parent.name == run_id:
                return payload
        return None

    def _git_run_from_record(self, record: dict) -> GitRun:
        """将 run.json 还原为可供 abort 使用的 GitRun。"""
        return GitRun(
            run_id=record["run_id"],
            mode=record.get("mode", "unknown"),
            scope=record.get("scope", str(self.scope)),
            repo_root=Path(record.get("repo_root", self.repo_root)),
            scope_path=Path(record.get("scope_path", self.wiki_dir)),
            run_dir=Path(record.get("run_dir", self.run_root / record["run_id"])),
            before_commit=record["before_commit"],
            after_commit=record.get("after_commit", ""),
            commit=record.get("commit", ""),
            status=record.get("status", "active"),
            changed_files=record.get("changed_files", []),
            metadata=record.get("metadata", {}) or {},
        )

    def abort_stale(self, run_id: str) -> GitRun:
        """回撤一个确认过的 stale active run。

        只允许锁对应进程已经退出的运行；调用方负责先向用户展示
        changed_files 并取得显式确认。
        """
        record = self.run_record(run_id)
        if record is None:
            raise GitManagerError(f"找不到运行记录: {run_id}")
        if record.get("status") != "active":
            raise GitManagerError(f"运行不是 active，不能使用 abort-stale: {record.get('status')}")
        status = self.stale_status()
        lock = status.get("lock")
        if lock and not lock.get("stale"):
            raise GitManagerError(f"运行锁仍由 pid={lock.get('pid')} 持有，拒绝回撤活动运行")
        if lock and lock.get("stale"):
            self._lock_path.unlink(missing_ok=True)
        self._acquire_lock()
        run = self._git_run_from_record(record)
        return self.abort(run, reason="manual stale run rollback")

    def diff_for_run(self, run_id: str) -> str:
        record = self.run_record(run_id)
        if record is None:
            raise GitManagerError(f"找不到运行记录: {run_id}")
        diff_path = Path(record["run_dir"]) / "diff.patch"
        if diff_path.exists():
            return diff_path.read_text(encoding="utf-8")
        commit = record.get("commit") or record.get("after_commit")
        if not commit:
            return ""
        return self._git("show", "--format=", commit, "--", self._scope_arg()).stdout

    def _release_lock(self) -> None:
        if self._lock_owned:
            self._lock_path.unlink(missing_ok=True)
            self._lock_owned = False

    def _write_run(self, run: GitRun) -> None:
        run.run_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(run)
        payload["repo_root"] = str(run.repo_root)
        payload["scope_path"] = str(run.scope_path)
        payload["run_dir"] = str(run.run_dir)
        (run.run_dir / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 生命周期

    def begin(self, run_id: str, *, mode: str, scope: str | Path | None = None) -> GitRun:
        """创建运行检查点；默认要求 Wiki scope 干净。"""
        requested = (self.wiki_dir / scope).resolve() if scope else self.wiki_dir
        try:
            requested.relative_to(self.wiki_dir)
        except ValueError as exc:
            raise GitScopeError(f"scope 越过 Wiki 根目录: {scope}") from exc
        if requested != self.wiki_dir:
            raise GitScopeError("第一版只允许以整个 wiki 作为事务 scope")
        if self.require_clean and self._status():
            raise GitWorkspaceDirty(
                "Wiki 工作区存在未提交修改，请先提交后再运行: " + "; ".join(self._status()[:8])
            )
        self._acquire_lock()
        try:
            now = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            run_dir = self.run_root / f"{mode}_{run_id or now}"
            run = GitRun(
                run_id=run_id or now,
                mode=mode,
                scope=str(self.scope),
                repo_root=Path(self.repo_root),
                scope_path=self.wiki_dir,
                run_dir=run_dir,
                before_commit=self._head(),
            )
            self._write_run(run)
            emit_event(
                "git_run_started",
                run_id=run.run_id,
                before_commit=run.before_commit,
                scope=str(self.scope),
            )
            return run
        except Exception:
            self._release_lock()
            raise

    def _changed_files(self, run: GitRun) -> list[str]:
        status = self._status()
        files: list[str] = []
        for line in status:
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path not in files:
                files.append(path)
        run.changed_files = files
        return files

    def _write_diff(self, run: GitRun) -> None:
        result = self._git("diff", "--", self._scope_arg())
        (run.run_dir / "diff.patch").write_text(result.stdout, encoding="utf-8")
        (run.run_dir / "changed_files.json").write_text(
            json.dumps(run.changed_files, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def commit(
        self,
        run: GitRun,
        *,
        message: str,
        metadata: dict | None = None,
        scan_report: str | Path | None = None,
        diff_report: str | Path | None = None,
        expected_files: list[str] | None = None,
    ) -> GitRun:
        """提交本次 Wiki 运行；只 stage Wiki scope。"""
        if run.status != "active":
            raise GitManagerError(f"运行已结束，不能提交: {run.status}")
        try:
            self._changed_files(run)
            self._write_diff(run)
            if expected_files is not None and sorted(run.changed_files) != sorted(expected_files):
                raise GitCommitError(
                    f"变更清单与预期不一致: actual={run.changed_files}, expected={expected_files}"
                )
            if scan_report is not None:
                report = Path(scan_report)
                if not report.is_file():
                    raise GitCommitError(f"scan 报告不存在，禁止提交: {report}")
                if not report.resolve().is_relative_to(run.run_dir.resolve()):
                    raise GitCommitError("scan 报告必须存放在本次 run 目录内")
                run.metadata["scan_report"] = str(report)
            if diff_report is not None:
                report = Path(diff_report)
                if not report.is_file():
                    raise GitCommitError(f"diff 报告不存在，禁止提交: {report}")
                if not report.resolve().is_relative_to(run.run_dir.resolve()):
                    raise GitCommitError("diff 报告必须存放在本次 run 目录内")
                run.metadata["diff_report"] = str(report)
            elif scan_report is None and run.changed_files:
                raise GitCommitError("存在 Wiki 变更但未绑定 scan 报告，禁止提交")
            if not run.changed_files:
                run.status = "committed"
                run.after_commit = self._head()
                run.commit = run.after_commit
                run.metadata.update(metadata or {})
                self._write_run(run)
                return run
            # 只暂存 begin 后记录的 Wiki 变更；工作区审计文件不在
            # Wiki 仓库内，也不能因后续生成而混入本次 commit。
            self._git("add", "--", *run.changed_files)
            staged = [
                path
                for path in self._git("diff", "--cached", "--name-only", "-z").stdout.split("\0")
                if path
            ]
            if sorted(staged) != sorted(run.changed_files):
                self._git("reset", "--", self._scope_arg(), check=False)
                raise GitCommitError(
                    "暂存变更清单与运行变更清单不一致: "
                    f"staged={staged}, changed={run.changed_files}"
                )
            if any(
                not (Path(self.repo_root) / path).resolve().is_relative_to(self.wiki_dir)
                for path in staged
            ):
                self._git("reset", "--", self._scope_arg(), check=False)
                raise GitScopeError("暂存区包含 Wiki scope 外文件")
            # pathspec 明确限制提交范围，避免仓库中其他已暂存内容被带入。
            result = self._git("commit", "-m", message, "--", self._scope_arg(), check=False)
            if result.returncode:
                self._git("reset", "--", self._scope_arg(), check=False)
                raise GitCommitError((result.stderr or result.stdout).strip())
            run.status = "committed"
            run.after_commit = self._head()
            run.commit = run.after_commit
            run.metadata.update(metadata or {})
            self._write_run(run)
            emit_event(
                "git_run_committed",
                run_id=run.run_id,
                before_commit=run.before_commit,
                commit=run.commit,
                changed_files=run.changed_files,
            )
            return run
        finally:
            self._release_lock()

    def abort(self, run: GitRun, *, reason: str) -> GitRun:
        """恢复当前未提交运行到 before_commit，随后释放锁。"""
        if run.status != "active":
            return run
        try:
            self._git(
                "restore",
                "--source",
                run.before_commit,
                "--staged",
                "--worktree",
                "--",
                self._scope_arg(),
            )
            # 只删除本次 scope 内、Git 未跟踪的文件；不调用无范围 git clean。
            # 使用 NUL 分隔，避免中文等路径被 core.quotePath 转义后无法定位。
            untracked = [
                path
                for path in self._git(
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                    "--",
                    self._scope_arg(),
                ).stdout.split("\0")
                if path
            ]
            for path in untracked:
                target = (Path(self.repo_root) / path).resolve()
                if target.is_file() and target.is_relative_to(self.wiki_dir):
                    target.unlink()
            run.status = "aborted"
            run.metadata["abort_reason"] = reason
            self._changed_files(run)
            self._write_run(run)
            emit_event("git_run_aborted", run_id=run.run_id, reason=reason)
            return run
        finally:
            self._release_lock()

    def rollback(self, commit: str, *, run_id: str = "") -> str:
        """对已提交的 Wiki commit 创建安全反向提交。"""
        if self._status():
            raise GitWorkspaceDirty("回撤前 Wiki 工作区必须干净")
        self._acquire_lock()
        try:
            result = self._git("revert", "--no-edit", commit, check=False)
            if result.returncode:
                self._git("revert", "--abort", check=False)
                raise GitManagerError((result.stderr or result.stdout).strip())
            new_commit = self._head()
            emit_event(
                "git_run_rolled_back",
                run_id=run_id,
                reverted_commit=commit,
                rollback_commit=new_commit,
            )
            return new_commit
        finally:
            self._release_lock()
