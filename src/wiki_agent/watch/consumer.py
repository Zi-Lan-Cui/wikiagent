"""消费端——Job handler：ingest 执行 + 成功核账凭证。

串行由 JobWorker 保证（一次一个 claim）；编译会更新 wiki 与工作区溯源
存档，并发会互相覆盖——串行是硬需求。

本模块不写"完成账"也不报失败 issue：
- 成功时把 pre/post 一致的 digest+text 放进 JobResult.detail，
  由 JobOutcomeHandler 在终态事务提交后写 WatchState（成功才落账、先库后文件）；
- 业务失败（IngestError）转成 failed/ingest_error 结果，issue 上报
  同样收口在 outcome；未预期异常裸抛，Worker 归 transient 链式退避。

源文件删除按确定性规则清理（纯代码，无 LLM）: 溯源记录只含被删文件 →
删除记录；还含其他文件 → 仅移除该条目。
"""

from __future__ import annotations

import re
from pathlib import Path

from wiki_agent.application.job_results import JobResult
from wiki_agent.compiler.workflows.failures import failure_diagnostics
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.documents.loader import DataLoader
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.jobs import Job
from wiki_agent.log import emit_event, get_logger
from wiki_agent.watch.state import WatchState, digest_file_text
from wiki_agent.wiki.frontmatter import split_frontmatter

logger = get_logger("WATCH_CONSUMER")


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


class WatchConsumer:
    """watch Job 的执行体——compile/delete 两类 handler。"""

    def __init__(
        self,
        pipeline: CompilePipeline,
        state: WatchState,
        *,
        wiki_dir: str | Path,
        source_records_dir: str | Path | None = None,
    ):
        self._pipeline = pipeline
        self._state = state
        self._wiki_dir = Path(wiki_dir)
        self._source_records_dir = (
            Path(source_records_dir)
            if source_records_dir is not None
            else self._wiki_dir.parent / "workspace" / "provenance" / "sources"
        )

    async def handle_job(self, job: Job, progress) -> JobResult:
        """执行一个 watch Job，返回业务结局（bug 才抛）。"""
        progress("load")
        if job.kind == "delete":
            return self._handle_delete(job)
        if job.kind != "compile":
            raise ValueError(f"unsupported watch job: {job.kind}")
        return await self._handle_compile(job, progress)

    # delete

    def _handle_delete(self, job: Job) -> JobResult:
        """删除任务：文件复活则跳过清理；state 条目由成功账处理删除。"""
        path = Path(job.resource)
        if path.exists():
            # 源文件复活——取消清理，条目照常删除：复活内容会被扫描
            # 当作新文件两段确认重新接入，避免拿着旧指纹误判"已处理"
            logger.info("  源文件复活，跳过删除清理: %s", path.name)
            return JobResult(status="succeeded")
        self._process_delete(path.name)
        return JobResult(status="succeeded")

    def _process_delete(self, name: str) -> None:
        """源文件删除 → 溯源存档清理（消费者职责）。

        Args:
            name: 被删源文件名。
        """
        wiki = self._wiki_dir
        src_dir = self._source_records_dir
        if not src_dir.is_dir():
            return
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
                page.write_text(new_content, encoding="utf-8")
                action = f"keep {slug}（sources 移除 {name}）"
            else:
                # 规则 2: 只剩被删文件 → 删除页面 + 正文引用换别名
                page.unlink()
                cleaned = clean_body_links(wiki, slug)
                action = f"delete {slug}（清理 {cleaned} 处正文引用）"
            logger.info("  %s", action)
            emit_event("watch_source_deleted", file=name, action=action)

    # compile

    async def _handle_compile(self, job: Job, progress) -> JobResult:
        """ingest 一个源文件；成功时携带经 pre/post 双检的核账凭证。"""
        path = Path(job.resource)
        payload_digest = str(job.payload.get("digest") or "")

        read = digest_file_text(path)
        if read is None:
            return self._ingest_error_result(
                job, IngestError(IngestStage.LOAD, f"源文件已消失: {path.name}", source=path.name)
            )
        digest_pre, text_pre = read

        # 幂等短路：该内容已确认完成（崩溃重放/重复提交），不再过 LLM
        if payload_digest and self._state.matches(str(path), payload_digest):
            emit_event("watch_skipped", file=path.name, reason="already_ingested")
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

        if outcome.noop:
            emit_event("watch_noop", file=path.name)
        else:
            emit_event("watch_ingested", file=path.name, pages=len(outcome.pages_written))

        # 核账凭证：pre（本次实际读到的）与 post（ingest 后未再变动）必须
        # 同时等于 job 请求的 digest——执行期间内容翻动/前进都拒绝落账，
        # 后继 Job 由提交侧保证存在
        post = digest_file_text(path)
        if payload_digest and post is not None and digest_pre == post[0] == payload_digest:
            return JobResult(status="succeeded", detail={"digest": payload_digest, "text": post[1]})
        if not payload_digest:
            # 升级前的旧行没有 digest——无法核账；本轮不落账，扫描会以带
            # digest 的后继收敛（最坏一次重复 ingest）
            return JobResult(status="succeeded")
        logger.info("  %s: 执行期间内容已前进/错配，不落账由后继 Job 接管", path.name)
        return JobResult(status="succeeded")

    def _ingest_error_result(self, job: Job, exc: IngestError) -> JobResult:
        """业务失败 → 结果化（issue 上报与事件在 outcome/此处收口）。"""
        name = Path(job.resource).name
        logger.error("  ingest 失败 [%s]: %s", exc.stage.value, str(exc)[:200])
        # 事件是机器通道——全量不截断（截断是给人看的习惯）
        emit_event(
            "watch_failure",
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
