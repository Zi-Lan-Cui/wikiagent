"""WikiWriteSession——写 wiki 的 job 共用的执行协议（上下文管理器形式）。

    with session.open(job) as write:
        ...执行业务、过质量闸门...
        write.commit(subject)   # 成功：显式结算，HEAD 前移（payload.batch 进尾注）
    或  write.abort_export()    # 业务失败：未提交改动导出 debris 证据后 restore

出口不变量：离开上下文时工作区必收敛回 HEAD——

- 已 commit / abort_export：按显式语义收场；
- 异常外抛（程序错误的通道，Worker 记日志与事件承接）：restore；
- handler 写了改动却没有宣布结局：restore 兜住。

协议的机械端点（进入时的 pre-reset、离开时的收敛）由上下文保证，不可能
被遗忘；只剩 commit / abort_export 两个业务决策由 handler 显式选择——
commit 的时机含义是"质量闸门已通过，这批改动值得成为 HEAD 的下一步"，
只能由执行体宣布。

SyncConsumer（compile/delete）与 WikiOpsConsumer（refine/restructure）
共用这一协议。commit subject 用操作语义前缀（sync:/retry:/refine:/
restructure:），payload.batch 进 commit 尾注——"撤销这一批"按尾注选段
revert。失败导出的 patch 与 commit 批尾注都是执行记录，不随 wiki 回退
删除。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.jobs import Job
from wiki_agent.log import emit_event

if TYPE_CHECKING:
    from wiki_agent.versioning import WikiGitManager


class Subject(StrEnum):
    """wiki commit 的 subject 前缀家族（与撤销语义对应）。"""

    SYNC = "sync"  # 快照批（compile 与 delete 共用，delete 拼作 "sync: delete <名>"）
    RETRY = "retry"  # issue retry 的 compile
    REFINE = "refine"  # 单页精炼
    RESTRUCTURE = "restructure"  # 重组执行单元


def commit_subject(prefix: Subject, target: str) -> str:
    """唯一的 subject 拼法——前缀家族只在这四处产生。"""
    return f"{prefix.value}: {target}"


# 未提交改动证据的存放目录名——provenance/sources 的同级目录，内部布局不是配置项
DEBRIS_DIRNAME = "debris"


def debris_dir_for(source_records_dir: str | Path) -> Path:
    """由档案目录推导未提交改动存放目录。"""
    return Path(source_records_dir).parent / DEBRIS_DIRNAME


def _restore(git: WikiGitManager | None) -> None:
    """把未提交改动收敛回 HEAD（git=None 时 no-op）。"""
    if git is not None:
        git.restore()


class WikiWrite:
    """session.open() 交给 handler 的句柄：两个业务决策点 + 出口状态。"""

    def __init__(
        self, git: WikiGitManager | None, debris_dir: Path | None, job: Job
    ) -> None:
        self._git = git
        self._debris_dir = debris_dir
        self._job = job
        self.commit_hash = ""
        self._settled = False

    @property
    def settled(self) -> bool:
        """handler 是否已宣布结局（commit 或 abort_export 走完）。"""
        return self._settled

    def commit(self, subject: str) -> str:
        """成功结算：wiki commit，payload.batch 进尾注；无变更返回空串。

        commit_all 抛出时不置 settled——没有提交就没有结算，离开上下文
        时由出口兜底 restore。
        """
        commit_hash = ""
        if self._git is not None:
            batch = str(self._job.payload.get("batch") or "")
            body = f"Batch: {batch}" if batch else ""
            commit_hash = self._git.commit_all(subject, body=body) or ""
        self.commit_hash = commit_hash
        self._settled = True
        return commit_hash

    def abort_export(self) -> None:
        """业务失败：未提交改动导出 debris 证据（diff 不进 git 历史）后 restore。

        导出或 restore 抛出时同样不置 settled，交给出口兜底再收敛一次。
        """
        self._export_debris()
        _restore(self._git)
        self._settled = True

    def restore_if_unsettled(self) -> None:
        """出口收拾：结局未宣布（异常外抛或 handler 漏结算）时收敛回 HEAD。"""
        if not self._settled:
            _restore(self._git)

    def _export_debris(self) -> None:
        if self._git is None:
            return
        patch = self._git.working_patch()
        if patch.strip() and self._debris_dir is not None:
            self._debris_dir.mkdir(parents=True, exist_ok=True)
            debris_file = self._debris_dir / f"{self._job.id}.patch"
            debris_file.write_text(patch, encoding="utf-8")
            emit_event("wiki_debris_saved", job_id=self._job.id, patch=str(debris_file))


class WikiWriteSession:
    """一个执行进程对 wiki 的写入会话（git=None 时全部动作退化为 no-op，
    供离线单测的裸 handler 装配）。"""

    def __init__(self, git: WikiGitManager | None, *, debris_dir: str | Path | None = None):
        self._git = git
        self._debris_dir = Path(debris_dir) if debris_dir is not None else None

    @contextmanager
    def open(self, job: Job) -> Generator[WikiWrite]:
        """进入即 pre-reset：把上一个失败/崩溃 job 的未提交改动收敛到 HEAD。

        离开时按出口不变量收敛（见模块 docstring）；restore 幂等，重复
        收敛无副作用。
        """
        _restore(self._git)
        write = WikiWrite(self._git, self._debris_dir, job)
        try:
            yield write
        finally:
            write.restore_if_unsettled()
