"""WikiWriteSession——写 wiki 的 job 共用的执行协议。

    pre-reset → 执行 → 成功 commit（批尾注）/ 失败导出未提交改动后 restore

SyncConsumer（compile/delete）与 WikiOpsConsumer（refine/restructure）都走
这一协议：wiki 机器管理，工作区的未提交改动都出自失败或中断的任务、
可以一律清除，HEAD 因此始终等于最近已结算状态。
commit subject 用操作语义前缀（sync:/retry:/refine:/restructure:），
payload.batch 进 commit 尾注——"撤销这一批"按尾注选段 revert。
未提交改动 patch 与批尾注同属留痕面，进程内永不回撤。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.jobs import Job
from wiki_agent.log import emit_event


class Subject(StrEnum):
    """wiki commit 的 subject 前缀家族（与撤销语义对应）。"""

    SYNC = "sync"  # 快照批（compile 与 delete 共用，delete 拼作 "sync: delete <名>"）
    RETRY = "retry"  # issue retry 的 compile
    REFINE = "refine"  # 单页精炼
    RESTRUCTURE = "restructure"  # 重组执行单元


def commit_subject(prefix: Subject, target: str) -> str:
    """唯一的 subject 拼法——前缀家族只在这四处产生。"""
    return f"{prefix.value}: {target}"

if TYPE_CHECKING:
    from wiki_agent.versioning import WikiGitManager

# 未提交改动 patch 目录名——provenance/sources 的同级目录，内部布局不是配置项
DEBRIS_DIRNAME = "debris"


def debris_dir_for(source_records_dir: str | Path) -> Path:
    """由档案目录推导未提交改动存放目录。"""
    return Path(source_records_dir).parent / DEBRIS_DIRNAME


class WikiWriteSession:
    """一个执行进程对 wiki 的写入会话（git=None 时全部动作退化为 no-op，
    供离线单测的裸 handler 装配）。"""

    def __init__(self, git: WikiGitManager | None, *, debris_dir: str | Path | None = None):
        self._git = git
        self._debris_dir = Path(debris_dir) if debris_dir is not None else None

    def pre_reset(self) -> None:
        """执行前把上一个失败/崩溃 job 的未提交改动收敛到 HEAD。"""
        if self._git is not None:
            self._git.restore()

    def commit(self, job: Job, subject: str) -> str:
        """成功结算的 wiki commit；noop 无变更返回空串（不造空提交）。"""
        if self._git is None:
            return ""
        batch = str(job.payload.get("batch") or "")
        body = f"Batch: {batch}" if batch else ""
        return self._git.commit_all(subject, body=body) or ""

    def discard_debris(self, job_id: str) -> None:
        """失败撤销：restore 前导出未提交改动 diff（证据进留痕面，内容不进历史）。"""
        if self._git is None:
            return
        patch = self._git.working_patch()
        if patch.strip() and self._debris_dir is not None:
            self._debris_dir.mkdir(parents=True, exist_ok=True)
            debris_file = self._debris_dir / f"{job_id}.patch"
            debris_file.write_text(patch, encoding="utf-8")
            emit_event("wiki_debris_saved", job_id=job_id, patch=str(debris_file))
        self._git.restore()
