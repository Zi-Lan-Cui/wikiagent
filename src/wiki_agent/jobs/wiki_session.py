"""写 wiki 的 job 共用的执行协议（上下文管理器形式）。

    with session.open(job) as write:
        ...执行业务、过质量检查...
        write.commit(subject)   # 成功：提交，HEAD 前移（commit body 记 batch）
    或  write.abort_export()    # 业务失败：未提交改动导出为 debris patch 后 restore

出口不变量：离开上下文时工作区必回退到 HEAD——

- 已 commit 或 abort_export：按显式结局处理；
- 异常外抛：restore；
- handler 改了 wiki 但未调用 commit/abort_export：由出口 restore 回退。

进入时 pre-reset、离开时回退由上下文保证，handler 只需选择 commit 或
abort_export——commit 表示质量检查已通过、这批改动应成为 HEAD。

SourceJobHandler（compile/delete）与 WikiOpsHandler（restructure/link）共用此
协议。commit subject 带操作前缀（sync:/retry:/sync: delete/restructure:/link:），
commit body 记 batch，按批撤销时据此定位。失败导出的 patch 与 commit 记录不随
wiki 回退删除。
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
    """commit subject 的操作前缀，按它定位批量撤销。"""

    SYNC = "sync"  # 快照批（compile 与 delete 共用，delete 写作 "sync: delete <名>"）
    RETRY = "retry"  # issue retry 的 compile
    RESTRUCTURE = "restructure"  # 重组单元（subject 直写 in → out 页清单）
    LINK = "link"  # 单页出链维护


def commit_subject(prefix: Subject, target: str) -> str:
    """subject 的统一拼法。"""
    return f"{prefix.value}: {target}"


# 未提交改动导出目录名，位于 source_records_dir 同级
DEBRIS_DIRNAME = "debris"


def debris_dir_for(source_records_dir: str | Path) -> Path:
    """由档案目录推导未提交改动导出目录。"""
    return Path(source_records_dir).parent / DEBRIS_DIRNAME


def _restore(git: WikiGitManager | None) -> None:
    """把未提交改动回退到 HEAD（git=None 时空操作）。"""
    if git is not None:
        git.restore()


class WikiWrite:
    """open() 交给 handler 的句柄：commit/abort 两个决策 + 是否已结算。"""

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
        """handler 是否已结算（commit 或 abort_export 完成）。"""
        return self._settled

    def commit(self, subject: str) -> str:
        """成功时提交 wiki，commit body 记 batch；无变更返回空串。

        commit_all 抛出时不置 settled，离开上下文由出口回退。
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
        """业务失败：未提交改动导出为 debris patch（不进 git 历史）后回退。

        抛出时不置 settled，离开上下文再回退一次。
        """
        self._export_debris()
        _restore(self._git)
        self._settled = True

    def restore_if_unsettled(self) -> None:
        """未结算时（异常外抛或 handler 漏调 commit/abort）回退到 HEAD。"""
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
    """一个进程的 wiki 写入会话（git=None 时各动作为空操作，供离线测试装配）。"""

    def __init__(self, git: WikiGitManager | None, *, debris_dir: str | Path | None = None):
        self._git = git
        self._debris_dir = Path(debris_dir) if debris_dir is not None else None

    @contextmanager
    def open(self, job: Job) -> Generator[WikiWrite]:
        """进入时先回退：清掉上一个失败/崩溃 job 遗留的未提交改动，回到 HEAD。

        离开时按出口不变量回退（见模块 docstring）；restore 幂等。
        """
        _restore(self._git)
        write = WikiWrite(self._git, self._debris_dir, job)
        try:
            yield write
        finally:
            write.restore_if_unsettled()
