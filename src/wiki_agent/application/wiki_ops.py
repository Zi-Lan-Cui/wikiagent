"""restructure / link 执行体——与 SourceJobHandler 同一 wiki 写协议。

with session.open(job) as write: 执行 → 成功 write.commit() /
业务失败 write.abort_export()（jobs.wiki_session 的出口不变量兜底）。
两类 job 与 sync job 同一队列串行执行；提交层的互斥与基线检查保证
执行时盘面与声明依据一致。

职责划界：restructure 动页面集合与内容（结构手术 + 触及页逐页成文），
link 只动出链（字面替换清单经代码校验应用，不重写正文）。失败的承接面
是 task failed + 日志 + 事件，不进问题账本——批操作结果不是"源材料等
人修"的待办，想再试就再提交一次。落盘前的一切失败不产生工作区改动，
直接返回 failed；apply_unit 写盘之后只有扫描闸门一条失败路径，
它走 abort_export（证据进 debris）后失败。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.compiler.content_pages import all_content_slugs
from wiki_agent.compiler.link import apply_link_fixes, plan_link_fixes
from wiki_agent.compiler.restructure import (
    RouteError,
    Unit,
    UnitMismatchError,
    apply_unit,
    prepare_unit,
    rewrite_unit_page,
)
from wiki_agent.compiler.restructure.plan import PAGE_TYPE_BY_DIR
from wiki_agent.jobs import Job, JobResult, Kind, Settlement
from wiki_agent.jobs.wiki_session import (
    Subject,
    WikiWriteSession,
    commit_subject,
    debris_dir_for,
)
from wiki_agent.log import emit_event, get_logger
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.normalize import normalize_page
from wiki_agent.wiki.quality import scan_wiki

if TYPE_CHECKING:
    from wiki_agent.jobs.worker import JobWorker
    from wiki_agent.versioning import WikiGitManager

logger = get_logger("WIKI_OPS")

_FM_REQUIRED = ("type", "title", "summary", "goal")


def _fill_frontmatter(text: str, skeleton: dict) -> str:
    """成文输出缺必填字段时用代码骨架补齐——模型丢字段不值得整单元失败。

    只动缺失的必填 key：有行则替换值，无行则插入 frontmatter 末尾；
    模型已给的值一律保留。骨架来自 _frontmatter_for（旧页=旧
    frontmatter，新页=由 intent 派生），与草稿同源。
    """
    fm, _ = split_frontmatter(text)
    missing = {
        k: str(skeleton[k])
        for k in _FM_REQUIRED
        if not str(fm.get(k) or "").strip() and str(skeleton.get(k) or "").strip()
    }
    if not missing:
        return text
    lines = text.splitlines(keepends=True)
    dash = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    if not lines[0].strip() == "---" or len(dash) < 2:
        return text  # 无合法 frontmatter，交给 normalize 报错
    replaced = set()
    for i in range(dash[0] + 1, dash[1]):
        key, _, _ = lines[i].partition(":")
        if key.strip() in missing:
            lines[i] = f'{key.strip()}: {json.dumps(missing[key.strip()], ensure_ascii=False)}\n'
            replaced.add(key.strip())
    insert_at = dash[1]
    for k, v in missing.items():
        if k not in replaced:
            lines.insert(insert_at, f"{k}: {json.dumps(v, ensure_ascii=False)}\n")
            insert_at += 1
    return "".join(lines)


class WikiOpsHandler:
    """restructure/link 两类 job 的执行体（经 register_jobs 认领）。"""

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
        """声明认领的 kind——restructure 与 link 的执行体都在本类。"""
        worker.register(Kind.RESTRUCTURE, self.handle_restructure)
        worker.register(Kind.LINK, self.handle_link)

    # restructure——一个单元一个任务一笔提交

    async def handle_restructure(self, job: Job, progress) -> JobResult:
        """执行一个重组单元：核对 → 路由 → 装配 → 逐页成文 → 落盘收尾 → 扫描闸。

        单元失败等于从未发生（落盘前失败无工作区改动；落盘后失败由
        abort_export 撤销并入 debris 证据）。
        """
        raw_unit = job.payload.get("unit")
        if not isinstance(raw_unit, dict):
            return JobResult(status="failed", detail={"error": "单元声明损坏: payload 缺 unit 对象"})
        try:
            unit = Unit.from_dict(raw_unit)
        except (TypeError, ValueError) as exc:
            return JobResult(status="failed", detail={"error": f"单元声明损坏: {exc}"[:500]})
        with self._session.open(job) as write:
            progress("verify")
            try:
                plan = await prepare_unit(self._wiki_dir, unit, self._llm)
            except UnitMismatchError as exc:
                # 单页改写（A→A）的目标页消失：排队期间被删，正常了结
                if len(unit.in_pages) == 1 and unit.in_pages == unit.out_slugs:
                    emit_event("restructure_skipped", unit=str(unit.in_pages), reason="unit_missing")
                    return JobResult(
                        status="succeeded", detail={"settlement": Settlement.UNIT_MISSING}
                    )
                emit_event("restructure_mismatch", job_id=job.id, detail=str(exc))
                return JobResult(
                    status="failed",
                    detail={"error": f"核对不过（{exc}）——声明落不上当前盘面，请重新分析"},
                )
            except RouteError as exc:
                emit_event("restructure_reverted", job_id=job.id, reason="route", detail=str(exc))
                return JobResult(status="failed", detail={"error": f"章节分配失败: {exc}"[:500]})

            progress("rewrite")
            contents: dict[str, str] = {}
            failures: list[str] = []
            for page in unit.out:
                if not page.polish:
                    continue  # 装配草稿即成品
                try:
                    text = await rewrite_unit_page(
                        self._llm,
                        slug=page.slug,
                        intent=page.intent,
                        draft=plan.drafts[page.slug],
                        old=plan.old_content[page.slug],
                        siblings=plan.sibling_titles[page.slug],
                    )
                except Exception as exc:
                    failures.append(f"{page.slug}: rewrite {str(exc)[:120]}")
                    continue
                text = _fill_frontmatter(text, plan.old_meta[page.slug])
                normalized, issues = normalize_page(
                    text,
                    path=f"{page.slug}.md",
                    valid_slugs=plan.final_slugs,
                    source_identity="",
                    today=date.today().isoformat(),
                    existing=plan.old_meta[page.slug],
                    # 类型由目录派生强制覆盖——成文模型偶发把目录名写进 type
                    page_type=PAGE_TYPE_BY_DIR[page.slug.split("/", 1)[0]],
                )
                errors = [issue for issue in issues if issue.level == "error"]
                if errors:
                    failures.append(
                        f"{page.slug}: normalize {'; '.join(str(i) for i in errors[:2])[:200]}"
                    )
                    continue
                contents[page.slug] = normalized
            if failures:  # 落盘未开始，整单元直接失败
                emit_event("restructure_reverted", job_id=job.id, reason="rewrite", detail="; ".join(failures)[:300])
                return JobResult(status="failed", detail={"error": "; ".join(failures)[:500]})

            progress("apply")
            apply_unit(self._wiki_dir, unit, plan, contents)

            progress("scan")
            errors = [issue for issue in scan_wiki(self._wiki_dir) if issue.level == "error"]
            if errors:
                write.abort_export()
                reason = "; ".join(str(issue) for issue in errors[:3])
                emit_event("restructure_reverted", job_id=job.id, reason="scan_gate", detail=reason[:300])
                logger.error("  扫描闸门不过，单元撤销: %s", reason)
                return JobResult(status="failed", detail={"error": f"扫描闸门不过: {reason}"[:500]})

            progress("commit")
            if unit.out:
                subject = commit_subject(
                    Subject.RESTRUCTURE,
                    f"{'+'.join(unit.in_pages)} → {'+'.join(unit.out_slugs)}",
                )
            else:
                subject = commit_subject(Subject.RESTRUCTURE, f"delete {'+'.join(unit.in_pages)}")
            commit = write.commit(subject)
            detail: dict[str, object] = {
                "settlement": Settlement.APPLIED,
                "assignment": len(plan.assignment),
                "fixed_by_take": len(plan.fixed),
            }
            if commit:
                detail["commit"] = commit
            return JobResult(status="succeeded", detail=detail)

    # link——一页一个任务，互不连坐

    async def handle_link(self, job: Job, progress) -> JobResult:
        """维护一页的出链：LLM 出替换清单，代码校验后应用；无可补即空操作。"""
        slug = str(job.payload.get("slug") or "")
        page = self._wiki_dir / f"{slug}.md"
        with self._session.open(job) as write:
            if not slug or not page.is_file():
                # 排队期间被重组掉——link 是当前状态的函数，页不在即了结
                emit_event("link_skipped", slug=slug, reason="gone_after_submit")
                return JobResult(
                    status="succeeded", detail={"settlement": Settlement.UNIT_MISSING}
                )
            progress("plan")
            replacements = await plan_link_fixes(self._llm, self._wiki_dir, slug)
            if replacements is None:
                return JobResult(status="failed", detail={"error": "link 清单生成失败"})
            content = page.read_text(encoding="utf-8")
            valid_slugs = set(all_content_slugs(self._wiki_dir))
            new_content, applied, skipped = apply_link_fixes(
                content, replacements, valid_slugs=valid_slugs, self_slug=slug
            )
            detail: dict[str, object] = {
                "settlement": Settlement.LINKED,
                "applied": len(applied),
                "skipped": len(skipped),
            }
            if new_content == content:
                emit_event("link_noop", slug=slug, skipped=len(skipped))
                return JobResult(status="succeeded", detail=detail)
            fm, _ = split_frontmatter(content)
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
                return JobResult(status="failed", detail={"error": f"link 规范化不过: {reason}"[:500]})
            page.write_text(normalized, encoding="utf-8")
            commit = write.commit(commit_subject(Subject.LINK, slug))
            emit_event("link_applied", slug=slug, applied=len(applied), skipped=len(skipped))
            if commit:
                detail["commit"] = commit
            return JobResult(status="succeeded", detail=detail)
