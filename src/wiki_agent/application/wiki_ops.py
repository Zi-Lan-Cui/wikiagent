"""refine / restructure 执行体——与 SyncConsumer 同一 wiki 写协议。

pre-reset → 执行 → 成功 commit / 失败残骸导出 + restore（jobs.wiki_session）。
两类 job 由人显式触发、走同一队列，与 sync job 串行执行，互斥由泵保证。

失败不进问题账本：issue 账本的语义是"源材料的业务失败、等人修复后重试"；
refine/restructure 失败是手动批操作的结果——task 的 failed 状态、日志与
事件（restructure_reverted/refine_failure）就是承接面，想再试就再点一次。
成功侧一样"先文件后库"：commit 在 handler 内、终态落库前——崩溃窗口只
会重做（幂等键+重放收敛），不会丢内容。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.compiler.restructure import Proposal, execute
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.documents.loader import DataLoader
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.jobs import Job, JobResult, Settlement
from wiki_agent.jobs.wiki_session import (
    Subject,
    WikiWriteSession,
    commit_subject,
    debris_dir_for,
)
from wiki_agent.log import emit_event, get_logger
from wiki_agent.wiki.quality import scan_wiki

if TYPE_CHECKING:
    from wiki_agent.versioning import WikiGitManager

logger = get_logger("WIKI_OPS")


def proposals_from_payload(payload: dict) -> list[Proposal]:
    """job payload → Proposal 对象（提交侧用 asdict 序列化，这里还原）。"""
    raw = payload.get("proposals") or []
    return [Proposal(**item) for item in raw if isinstance(item, dict)]


class WikiOpsConsumer:
    """refine/restructure 两类 job 的 handler（注册进 JobWorker）。"""

    def __init__(
        self,
        refine_pipeline: CompilePipeline,
        *,
        wiki_dir: str | Path,
        source_records_dir: str | Path,
        git: WikiGitManager | None = None,
    ):
        self._pipeline = refine_pipeline
        self._wiki_dir = Path(wiki_dir)
        self._session = WikiWriteSession(git, debris_dir=debris_dir_for(source_records_dir))

    async def handle_refine(self, job: Job, progress) -> JobResult:
        """refine 一页：输入是 wiki 页面自身，成功一页一提交。"""
        self._session.pre_reset()
        page = Path(job.resource)
        if not page.is_file():
            # 排队期间被 sync 删除——页面本就不该再精炼，no-op 了结
            emit_event("refine_skipped", page=job.resource, reason="gone_after_snapshot")
            return JobResult(status="succeeded", detail={"settlement": Settlement.UNIT_MISSING})
        progress("load")
        summary = DataLoader().load([page])
        if not summary.files:
            return self._failed(job, IngestError(IngestStage.LOAD, "页面加载为空", source=page.name))
        progress("ingest")
        try:
            outcome = await self._pipeline.ingest_one(summary.files[0])
        except IngestError as exc:
            return self._failed(job, exc)
        slug = str(page.relative_to(self._wiki_dir)).removesuffix(".md")
        commit = self._session.commit(job, commit_subject(Subject.REFINE, slug))
        if outcome.noop:
            emit_event("refine_noop", page=slug)
        else:
            emit_event("refine_ingested", page=slug, pages=len(outcome.pages_written))
        detail: dict[str, object] = {"settlement": Settlement.REFINED}
        if commit:
            detail["commit"] = commit
        return JobResult(status="succeeded", detail=detail)

    async def handle_restructure(self, job: Job, progress) -> JobResult:
        """执行已确认的重组提议：结构操作风险最高，单元内自带 scan 闸门——
        error 或有 skipped 动作即撤销本单元。"""
        self._session.pre_reset()
        proposals = proposals_from_payload(job.payload)
        if not proposals:
            return JobResult(status="succeeded", detail={"settlement": Settlement.APPLIED})
        progress("execute")
        result = execute(self._wiki_dir, proposals)
        issues = scan_wiki(self._wiki_dir)
        errors = [i for i in issues if i.level == "error"]
        if errors or result.skipped:
            self._session.discard_debris(job.id)
            reason = (
                f"结构重组撤销: scan errors={len(errors)}, skipped={len(result.skipped)}"
            )
            emit_event(
                "restructure_reverted",
                job_id=job.id,
                errors=[str(i) for i in errors[:5]],
                skipped=list(result.skipped)[:5],
            )
            logger.error("  %s", reason)
            return JobResult(status="failed", detail={"error": reason})
        progress("commit")
        first = proposals[0]
        subject = commit_subject(Subject.RESTRUCTURE, f"{first.op} {'+'.join(first.pages)}")
        if len(proposals) > 1:
            subject += f" 等 {len(proposals)} 项"
        commit = self._session.commit(job, subject)
        emit_event(
            "restructure_done",
            job_id=job.id,
            actions=len(result.actions),
            backed_up=len(result.backed_up),
        )
        ok_detail: dict[str, object] = {"settlement": Settlement.APPLIED}
        if commit:
            ok_detail["commit"] = commit
            ok_detail["actions"] = str(len(result.actions))
        return JobResult(status="succeeded", detail=ok_detail)

    def _failed(self, job: Job, exc: IngestError | None) -> JobResult:
        """refine 业务失败：残骸撤销 + 事件，不记账（批操作结果非用户待办）。"""
        page = Path(job.resource).name
        self._session.discard_debris(job.id)
        message = str(exc)[:500] if exc is not None else "加载为空"
        emit_event("refine_failure", page=page, error=message)
        logger.error("  refine 失败 %s: %s", page, message[:200])
        return JobResult(status="failed", detail={"error": message})
