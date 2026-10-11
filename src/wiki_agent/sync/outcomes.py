"""sync 线的终态联动规则：完成记录写入、溯源档案页落盘与删除清单应用。

SyncState 与档案页是文件，进不了 SQLite 事务——on_succeeded 返回 commit 后
要执行的动作清单，由队列在提交后立即执行；崩溃时靠 recover_stale 与 digest
幂等重做收敛。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.log import get_logger
from wiki_agent.queue import Job, JobResult, Kind, Settlement
from wiki_agent.queue.outcomes import JobOutcomeHandler
from wiki_agent.sync.state import SyncState

if TYPE_CHECKING:
    from wiki_agent.compiler.extraction import ExtractionSourcePageWriter

logger = get_logger("SYNC_OUTCOMES")


class _SyncOutcomeRules:
    """compile/delete 成功后落文件侧凭证；其余类别不注册到本规则。"""

    def __init__(
        self,
        *,
        state: SyncState,
        source_records_dir: str | Path | None,
        source_writer: ExtractionSourcePageWriter | None,
    ):
        self._state = state
        self._records_dir = Path(source_records_dir) if source_records_dir is not None else None
        self._source_writer = source_writer

    def on_succeeded(
        self,
        job: Job,
        result: JobResult,
        settlement: Settlement | None,
        conn: sqlite3.Connection,
    ) -> list[Callable[[], None]]:
        if job.kind == Kind.DELETE:
            # 删除结算：应用运行期只规划的档案清理清单 + 移除 state 条目
            archive_ops = result.detail.get("archive_ops")

            def settle_delete() -> None:
                for op in archive_ops if isinstance(archive_ops, list) else []:
                    self._apply_archive_op(op)
                self._state.drop(job.resource)
                self._state.save()

            return [settle_delete]
        digest = str(result.detail.get("digest") or "")
        text = result.detail.get("text")
        if job.kind != Kind.COMPILE or not digest or not isinstance(text, str):
            return []
        page = result.detail.get("source_page")

        def settle_compile() -> None:
            if isinstance(page, dict) and page.get("slug") and page.get("content"):
                if self._records_dir is None or self._source_writer is None:
                    logger.warning(
                        "缺 source_records_dir/source_writer，档案页未落盘: %s", job.resource
                    )
                else:
                    self._source_writer.write(
                        self._records_dir, str(page["slug"]), str(page["content"])
                    )
            self._state.record(job.resource, digest, text)

        return [settle_compile]

    @staticmethod
    def _apply_archive_op(op: object) -> None:
        """执行 delete job 规划的档案改写/移除。

        档案页在 wiki 的 git 作用域外、不受回滚保护，故与 issue 变更同点写入。
        """
        if not isinstance(op, dict):
            return
        target = Path(str(op.get("path") or ""))
        if op.get("action") == "unlink":
            target.unlink(missing_ok=True)
        elif op.get("action") == "rewrite" and target.is_file():
            target.write_text(str(op.get("content") or ""), encoding="utf-8")


def register_sync_outcomes(
    outcomes: JobOutcomeHandler,
    *,
    sync_state: SyncState,
    source_records_dir: str | Path | None = None,
    source_writer: ExtractionSourcePageWriter | None = None,
) -> None:
    """编译与删除的成功行落完成记录与档案；无 SyncState 的装配不注册。

    规则挂"任意成功"而非按 settlement 类别——handler 未申报结算时完成账
    仍要落（幂等重放语义），由规则内部按 job.kind 分流。
    """
    rules = _SyncOutcomeRules(
        state=sync_state, source_records_dir=source_records_dir, source_writer=source_writer
    )
    outcomes.register_on_success(rules)
