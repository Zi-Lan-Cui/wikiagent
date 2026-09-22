"""WikiWriteSession——写 wiki 的 job 共用的执行协议。

    pre-reset → 执行 → 成功 commit（批尾注）/ 失败残骸导出 + restore

SyncConsumer（compile/delete）与 WikiOpsConsumer（refine/restructure）都走
这一协议：wiki 机器管理、未提交即残骸，HEAD 永远等于最近已结算状态。
commit subject 用操作语义前缀（sync:/retry:/refine:/restructure:），
payload.batch 进 commit 尾注——"撤销这一批"按尾注选段 revert。
残骸 patch 与批尾注同属留痕面，进程内永不回撤。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.jobs import Job
from wiki_agent.log import emit_event

if TYPE_CHECKING:
    from wiki_agent.versioning import WikiGitManager


class WikiWriteSession:
    """一个执行进程对 wiki 的写入会话（git=None 时全部动作退化为 no-op，
    供离线单测的裸 handler 装配）。"""

    def __init__(self, git: WikiGitManager | None, *, debris_dir: str | Path | None = None):
        self._git = git
        self._debris_dir = Path(debris_dir) if debris_dir is not None else None

    def pre_reset(self) -> None:
        """执行前把上一个失败/崩溃 job 的残骸收敛到 HEAD。"""
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
        """失败撤销：restore 前导出残骸 diff（证据进留痕面，内容不进历史）。"""
        if self._git is None:
            return
        patch = self._git.working_patch()
        if patch.strip() and self._debris_dir is not None:
            self._debris_dir.mkdir(parents=True, exist_ok=True)
            debris_file = self._debris_dir / f"{job_id}.patch"
            debris_file.write_text(patch, encoding="utf-8")
            emit_event("wiki_debris_saved", job_id=job_id, patch=str(debris_file))
        self._git.restore()
