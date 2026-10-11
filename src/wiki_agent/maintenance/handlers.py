"""维护线三类 job（restructure / link / 结构分析）的执行体；重组的编排在 restructure.execution、提议的编排在 restructure.preview。

重组与补链和 SourceJobHandler 使用同一 wiki 写协议：
with session.open(job) as write: 执行，成功 write.commit()，
业务失败 write.abort_export()（queue.wiki_write 的出口不变量）。
维护线 job 与 sync job 在同一队列串行执行；提交层的互斥与基线检查保证
执行时 wiki 当前状态与提交时的声明依据一致。结构分析不写文件。

职责划分：restructure 修改页面集合与内容（结构调整 + 触及页逐页成文），
link 只修改出链（字面替换清单经代码校验后应用，不重写正文）。失败只体现为
task failed、日志与事件，不写入 issue 记录——批操作结果不属于待人工处理的
问题项，需要重试时重新提交即可。落盘前的任何失败都不产生工作区改动，直接
返回 failed；apply_unit 写盘之后只有质量扫描检查一条失败路径，
命中时走 abort_export（证据存入 debris）后返回 failed。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.log import emit_event, get_logger
from wiki_agent.maintenance.link import apply_link_fixes, plan_link_fixes
from wiki_agent.maintenance.restructure import Unit
from wiki_agent.maintenance.restructure.execution import execute_restructure_unit
from wiki_agent.queue import Job, JobResult, Kind, Settlement
from wiki_agent.queue.wiki_write import (
    Subject,
    WikiWriteSession,
    commit_subject,
    debris_dir_for,
)
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.normalize import normalize_page
from wiki_agent.wiki.pages import all_content_slugs, path_for

if TYPE_CHECKING:
    from wiki_agent.queue.worker import JobWorker
    from wiki_agent.versioning import WikiGitManager

logger = get_logger("WIKI_OPS")

class MaintenanceJobHandler:
    """维护线三类 job（重组/补链/结构分析）的执行体，经 register_jobs 认领。"""

    def __init__(
        self,
        llm,
        *,
        wiki_dir: str | Path,
        source_records_dir: str | Path,
        git: WikiGitManager | None = None,
    ):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._session = WikiWriteSession(git, debris_dir=debris_dir_for(source_records_dir))

    def register_jobs(self, worker: JobWorker) -> None:
        """声明认领的 kind——维护线三类 job 的执行体都在本类。"""
        worker.register(Kind.RESTRUCTURE, self.handle_restructure)
        worker.register(Kind.LINK, self.handle_link)
        worker.register(Kind.RESTRUCTURE_PREVIEW, self.handle_preview)

    async def handle_preview(self, job: Job, progress) -> JobResult:
        """跑一遍提议流程并把建议清单写进任务结果；不碰任何文件。

        申报 PREVIEWED 结算，建议清单由 JobOutcomeHandler 在终态事务内
        写入维护提议账本，任务行的 result 只作归档回看。
        """
        from wiki_agent.maintenance.restructure.preview import preview_payload, propose_restructure

        outcome = await propose_restructure(self._llm, self._wiki_dir, progress=progress)
        return JobResult(
            status="succeeded",
            detail={"settlement": Settlement.PREVIEWED, "preview": preview_payload(outcome)},
        )

    async def handle_restructure(self, job: Job, progress) -> JobResult:
        """Job 协议适配：payload 解析、git 会话与 commit/abort 决定；编排见 restructure.execution。"""
        try:
            unit = Unit.from_dict(job.payload.get("unit"))
        except (TypeError, ValueError) as exc:
            return JobResult(status="failed", detail={"error": f"单元声明损坏: {exc}"[:500]})
        with self._session.open(job) as write:
            result = await execute_restructure_unit(
                self._llm, self._wiki_dir, unit, job_id=job.id, progress=progress
            )
            if result.kind == "target_missing":
                return JobResult(status="succeeded", detail={"settlement": Settlement.TARGET_MISSING})
            if result.kind == "applied":
                progress("提交版本")
                if unit.out:
                    target = f"{'+'.join(unit.in_pages)} → {'+'.join(unit.out_slugs)}"
                else:
                    target = f"delete {'+'.join(unit.in_pages)}"
                commit = write.commit(commit_subject(Subject.RESTRUCTURE, target))
                detail: dict[str, object] = {
                    "settlement": Settlement.APPLIED,
                    "assignment": result.assignment,
                    "fixed_by_take": result.fixed_by_take,
                }
                if commit:
                    detail["commit"] = commit
                return JobResult(status="succeeded", detail=detail)
            if result.kind == "scan":
                write.abort_export()
            return JobResult(status="failed", detail={"error": result.error})

    async def handle_link(self, job: Job, progress) -> JobResult:
        """维护一页的出链：LLM 出替换清单，代码校验后应用；无可补即空操作。"""
        slug = str(job.payload.get("slug") or "")
        page = path_for(self._wiki_dir, slug)
        with self._session.open(job) as write:
            if not slug or not page.is_file():
                # 排队期间被重组删除——link 结果只取决于当前状态，页不存在即按正常结束处理
                emit_event("link_skipped", slug=slug, reason="gone_after_submit")
                return JobResult(status="succeeded", detail={"settlement": Settlement.TARGET_MISSING})
            progress("核对出链")
            try:
                replacements = await plan_link_fixes(self._llm, self._wiki_dir, slug)
            except Exception as exc:
                return JobResult(
                    status="failed", detail={"error": f"link 清单生成失败: {str(exc)[:300]}"}
                )
            content = page.read_text(encoding="utf-8")
            fm, body = split_frontmatter(content)
            valid_slugs = set(all_content_slugs(self._wiki_dir))
            new_body, applied, skipped = apply_link_fixes(
                body, replacements, valid_slugs=valid_slugs, self_slug=slug
            )
            detail: dict[str, object] = {
                "settlement": Settlement.LINKED,
                "applied": len(applied),
                "skipped": len(skipped),
            }
            if new_body == body:
                emit_event("link_noop", slug=slug, skipped=len(skipped))
                return JobResult(status="succeeded", detail=detail)
            # 正文是 split_frontmatter 的连续后缀，按长度拼回即保留 frontmatter 原文
            new_content = content[: len(content) - len(body)] + new_body
            normalized, issues = normalize_page(
                new_content,
                path=f"{slug}.md",
                valid_slugs=valid_slugs,
                source_identity="",
                today=date.today().isoformat(),
                existing=fm,
            )
            errors = [issue for issue in issues if issue.level == "error"]
            if errors:
                # 只替换了链接、正文未落盘——返回 failed，工作区无改动
                reason = "; ".join(str(issue) for issue in errors[:3])
                emit_event("link_reverted", slug=slug, errors=reason[:300])
                logger.error("  link 撤销 %s: %s", slug, reason)
                return JobResult(
                    status="failed", detail={"error": f"link 规范化不过: {reason}"[:500]}
                )
            page.write_text(normalized, encoding="utf-8")
            commit = write.commit(commit_subject(Subject.LINK, slug))
            emit_event("link_applied", slug=slug, applied=len(applied), skipped=len(skipped))
            if commit:
                detail["commit"] = commit
            return JobResult(status="succeeded", detail=detail)
