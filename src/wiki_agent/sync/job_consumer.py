"""消费端——源文件 Job handler：per-job git 协议 + ingest 执行 + 成功凭证。

串行由 JobWorker 保证（一次一个 claim）；编译会更新 wiki 与工作区溯源
存档，并发会互相覆盖——串行是硬需求。

每个 compile/delete job 都走同一协议：

    pre-reset（工作区收敛到 HEAD）→ 执行 → 成功 commit / 失败 restore

wiki 是机器管理的，未提交内容只可能是残骸，因此 pre-reset 无条件安全；
崩溃现场来不及 restore 也由下一个 job 的 pre-reset 收编。HEAD 于是始终
等于"最近已结算状态"。失败 restore 前把残骸 diff 导出到
workspace/provenance/debris/ 留证据（日志/事件/残骸快照永不回撤）。

本模块不写"完成账"也不报失败 issue：
- 成功时把**本次实际读到的** digest+text（+档案页内容、commit）放进
  JobResult.detail，由 JobOutcomeHandler 在终态事务提交后写 SyncState
  与溯源档案（成功才落账、先库后文件）。快照语义下读到的内容可以与
  提交时的 payload.digest 不同——那是合法版本滞后，无需凭证校验；
- 快照后文件消失 → no-op succeeded（从未入账，无账可清、不算失败）；
- 业务失败（IngestError）→ 残骸快照 + restore，转成 failed/ingest_error
  结果；issue 上报收口在 outcome；未预期异常裸抛，由 Worker 归日志+事件。

源文件删除按确定性规则清理（纯代码，无 LLM）: 溯源记录只含被删文件 →
删除记录；还含其他文件 → 仅移除该条目。档案页在 git scope 外、不受回滚
保护，因此清理动作只规划成清单、交给结算落盘；wiki 正文的引用清理是
scope 内变更，随本次 commit 一起生效。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.compiler.workflows.failures import failure_diagnostics
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.documents.loader import DataLoader
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.jobs import Job, JobResult
from wiki_agent.log import emit_event, get_logger
from wiki_agent.sync.state import SyncState, digest_file_text
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.quality import scan_source

if TYPE_CHECKING:
    from wiki_agent.versioning import WikiGitManager

logger = get_logger("SYNC_CONSUMER")


def clean_body_links(wiki: Path, slug: str) -> int:
    """全库正文中 [[sources/<slug>|别名]] → 别名纯文本。

    Args:
        wiki: wiki 根目录。
        slug: 被删 sources 页 slug。

    Returns:
        清理的文件数。
    """
    changed = 0
    for sub in ("concepts", "entities", "topics"):
        d = wiki / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            content = p.read_text(encoding="utf-8")
            new_content = re.sub(
                rf"\[\[sources/{re.escape(slug)}(?:\|([^\]]+?))?\]\]",
                lambda m: m.group(1) if m.group(1) else slug.rsplit("/", 1)[-1],
                content,
            )
            if new_content != content:
                p.write_text(new_content, encoding="utf-8")
                changed += 1
                logger.info("  正文引用清理: %s", p.name)
    return changed


class SyncConsumer:
    """sync/重试 Job 的执行体——compile/delete 两类 handler。"""

    def __init__(
        self,
        pipeline: CompilePipeline,
        state: SyncState,
        *,
        wiki_dir: str | Path,
        source_records_dir: str | Path | None = None,
        git: WikiGitManager | None = None,
    ):
        self._pipeline = pipeline
        self._state = state
        self._wiki_dir = Path(wiki_dir)
        self._source_records_dir = (
            Path(source_records_dir)
            if source_records_dir is not None
            else self._wiki_dir.parent / "workspace" / "provenance" / "sources"
        )
        # git=None 只在离线单测里出现（无仓库环境的裸 handler 测试）
        self._git = git
        self._debris_dir = self._source_records_dir.parent / "debris"

    async def handle_job(self, job: Job, progress) -> JobResult:
        """执行一个源文件 Job，返回业务结局（bug 才抛）。"""
        if self._git is not None:
            # pre-reset：上一个失败/崩溃 job 的残骸先收敛到 HEAD 再动笔
            self._git.restore()
        progress("load")
        if job.kind == "delete":
            return self._handle_delete(job)
        if job.kind != "compile":
            raise ValueError(f"unsupported source job: {job.kind}")
        return await self._handle_compile(job, progress)

    # git 协议

    def _commit(self, job: Job, subject: str) -> str:
        """成功结算的 wiki commit；subject=语义名，批尾注供撤销批定位。

        noop 成功（无页面变化）返回空串——commit_all 无变更不造空提交。
        """
        if self._git is None:
            return ""
        batch = str(job.payload.get("batch") or "")
        body = f"Batch: {batch}" if batch else ""
        return self._git.commit_all(subject, body=body) or ""

    def _discard_debris(self, job_id: str) -> None:
        """失败/撤销前导出残骸 diff，再把 wiki 恢复到 HEAD。"""
        if self._git is None:
            return
        patch = self._git.working_patch()
        if patch.strip():
            self._debris_dir.mkdir(parents=True, exist_ok=True)
            debris_file = self._debris_dir / f"{job_id}.patch"
            debris_file.write_text(patch, encoding="utf-8")
            emit_event("sync_debris_saved", job_id=job_id, patch=str(debris_file))
        self._git.restore()

    # delete

    def _handle_delete(self, job: Job) -> JobResult:
        """删除任务：文件复活则跳过清理；state 条目由成功账处理删除。"""
        path = Path(job.resource)
        if path.exists():
            # 源文件复活——跳过清理，条目照常删除：复活内容相对账本是脏的，
            # 下一次 sync 快照会重新编译它，不需要在这里猜内容状态
            logger.info("  源文件复活，跳过删除清理: %s", path.name)
            return JobResult(status="succeeded")
        archive_ops = self._plan_archive_cleanup(path.name)
        commit = self._commit(job, f"sync: delete {path.name}")
        return JobResult(
            status="succeeded",
            detail={"archive_ops": archive_ops, "commit": commit}
            if archive_ops or commit
            else {},
        )

    def _plan_archive_cleanup(self, name: str) -> list[dict[str, str]]:
        """源文件删除 → 规划溯源档案清理（消费者职责）。

        档案页在 wiki/git scope 外、不受回滚保护：这里只返回改写/移除
        清单，落盘由 outcome 在成功结算时执行（与账本 drop 同点）。
        正文引用清理是 scope 内变更，立即生效、随本次 delete commit 入账，
        崩溃后重放幂等。

        Args:
            name: 被删源文件名。
        """
        wiki = self._wiki_dir
        src_dir = self._source_records_dir
        ops: list[dict[str, str]] = []
        if not src_dir.is_dir():
            return ops
        for page in sorted(src_dir.glob("*.md")):
            content = page.read_text(encoding="utf-8")
            fm, _ = split_frontmatter(content)
            raw_sources = fm.get("sources", "")
            listed = [
                s.strip().strip("\"'") for s in raw_sources.strip("[]").split(",") if s.strip()
            ]
            if name not in listed:
                continue
            remaining = [s for s in listed if s != name]
            slug = page.stem
            if remaining:
                # 规则 3: 保留页面，移除条目
                new_sources = ", ".join(f'"{s}"' for s in remaining)
                new_content = re.sub(
                    r"(?m)^\s*sources\s*:.*$",
                    f"sources: [{new_sources}]",
                    content,
                    count=1,
                )
                ops.append({"action": "rewrite", "path": str(page), "content": new_content})
                action = f"keep {slug}（sources 移除 {name}）"
            else:
                # 规则 2: 只剩被删文件 → 页面删除 + 正文引用换别名
                ops.append({"action": "unlink", "path": str(page)})
                cleaned = clean_body_links(wiki, slug)
                action = f"delete {slug}（清理 {cleaned} 处正文引用）"
            logger.info("  %s", action)
            emit_event("sync_source_deleted", file=name, action=action)
        return ops

    # compile

    async def _handle_compile(self, job: Job, progress) -> JobResult:
        """ingest 一个源文件；成功携带**本次实际读到内容**的核账凭证。

        快照语义：读到什么记什么——与 payload.digest 不同也照常落账
        （执行中文件被改是合法滞后，下一次 sync 追平），不再有 pre/post
        三方凭证校验。
        """
        path = Path(job.resource)

        read = digest_file_text(path)
        if read is None:
            # 快照后才消失：它从未进过账，无账可清也不该报失败——
            # succeeded 静默了结，下一次 sync 的差集会处理真正的删除
            emit_event("sync_skipped", file=path.name, reason="gone_after_snapshot")
            return JobResult(status="succeeded")
        digest, text = read

        # 幂等短路（廉价保险）：该快照内容已有完成账，不再烧 LLM
        payload_digest = str(job.payload.get("digest") or "")
        if payload_digest and self._state.matches(str(path), payload_digest):
            emit_event("sync_skipped", file=path.name, reason="already_ingested")
            return JobResult(status="succeeded", detail={})

        loader = DataLoader()
        summary = loader.load([path])
        if not summary.files:
            return self._ingest_error_result(
                job, IngestError(IngestStage.LOAD, "文件加载为空", source=path.name)
            )

        progress("ingest")
        try:
            outcome = await self._pipeline.ingest_one(summary.files[0])
        except IngestError as exc:
            return self._ingest_error_result(job, exc)

        # 单 source 局部质量闸门（原批壳的 scan_source 移进队列执行体）：
        # 检查本轮产出——生成页查结构/死链，档案页查内存内容（尚未落盘）。
        # error 即本 job 业务失败：残骸 restore、记账等人，不污染其他 source。
        page = outcome.extract.source_page if outcome.extract is not None else None
        local_issues = scan_source(
            self._wiki_dir,
            source_name=path.name,
            generated_paths=outcome.pages_written,
            source_page=(page.slug, page.content) if page is not None else None,
        )
        local_errors = [issue for issue in local_issues if issue.level == "error"]
        if local_errors:
            reason = "; ".join(str(issue) for issue in local_errors[:3])
            return self._ingest_error_result(
                job,
                IngestError(
                    IngestStage.EXECUTE,
                    f"单 source 质量检查失败: {reason}",
                    source=path.name,
                    error_code="source_quality_error",
                ),
            )

        # 成功：wiki 变更即刻 commit（HEAD 前移一步），账本与档案页随后由
        # outcome 结算——先文件后库的方向保证崩溃只会重做、不会丢内容。
        subject = "retry" if job.mode == "issue_retry" else "sync"
        commit = self._commit(job, f"{subject}: {path.name}")
        detail: dict[str, object] = {"digest": digest, "text": text}
        if commit:
            detail["commit"] = commit
        if page is not None:
            detail["source_page"] = {"slug": page.slug, "content": page.content}
        if outcome.noop:
            emit_event("sync_noop", file=path.name)
        else:
            emit_event("sync_ingested", file=path.name, pages=len(outcome.pages_written))
        return JobResult(status="succeeded", detail=detail)

    def _ingest_error_result(self, job: Job, exc: IngestError) -> JobResult:
        """业务失败 → 残骸快照+restore → 结果化（issue 上报收口在 outcome）。"""
        name = Path(job.resource).name
        logger.error("  ingest 失败 [%s]: %s", exc.stage.value, str(exc)[:200])
        self._discard_debris(job.id)
        # 事件是机器通道——全量不截断（截断是给人看的习惯）
        emit_event(
            "sync_failure",
            file=name,
            stage=exc.stage.value,
            error=str(exc),
            cause=type(exc.cause).__name__ if exc.cause else None,
            raw=exc.raw,
        )
        diagnostics, _ = failure_diagnostics(exc)
        return JobResult(
            status="failed",
            error_type="ingest_error",
            detail={
                "error": str(exc)[:500],
                "stage": exc.stage.value,
                "raw": exc.raw,
                "diagnostics": diagnostics,
                "source": name,
                "source_path": str(job.resource),
                "source_kind": "input_file",
                "retry_policy": exc.retry_policy,
                "mode": job.mode,
            },
        )
