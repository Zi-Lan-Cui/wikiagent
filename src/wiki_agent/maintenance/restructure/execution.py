"""restructure 执行管线：页面核对 → 逐页成文 → 规范化 → 落盘 → 质量扫描。

不碰 git 协议：commit / abort_export 的归 handler，本模块只产出执行结局
与数据。不变量：落盘前的任何失败不产生工作区改动；apply_unit 之后唯一
失败路径是扫描闸门，命中时由调用方走 abort_export 撤销（debris 留证）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from wiki_agent.log import emit_event, get_logger
from wiki_agent.wiki.frontmatter import render_frontmatter, set_fields, split_frontmatter
from wiki_agent.wiki.normalize import normalize_page
from wiki_agent.wiki.pages import PAGE_TYPE_BY_DIR
from wiki_agent.wiki.quality import scan_wiki

from . import (
    RouteError,
    Unit,
    UnitMismatchError,
    apply_unit,
    prepare_unit,
    rewrite_unit_page,
)

logger = get_logger("WIKI_OPS")

_FM_REQUIRED = ("type", "title", "summary", "goal")


def _fill_frontmatter(text: str, skeleton: dict) -> str:
    """成文输出缺必填字段时用代码骨架补齐，避免因丢字段使整单元失败。

    只补缺失的必填 key，模型已给的值一律保留；骨架来自
    _frontmatter_for（旧页=旧 frontmatter，新页=由 intent 派生），
    与草稿同源。输出没有 frontmatter（模型对拆分新页常返回纯正文
    片段）时整页由骨架合成，正文原样保留。
    """
    fm, _ = split_frontmatter(text)
    missing = {
        k: str(skeleton[k])
        for k in _FM_REQUIRED
        if not str(fm.get(k) or "").strip() and str(skeleton.get(k) or "").strip()
    }
    if not missing:
        return text
    dash = [ln for ln in text.splitlines() if ln.strip() == "---"]
    if text.startswith("---") and len(dash) >= 2:
        return set_fields(text, missing)
    fields = {k: v for k, v in skeleton.items() if str(v).strip()}
    fields.update(missing)
    return render_frontmatter(fields) + text



@dataclass
class RestructureExecution:
    """执行管线结局：kind 决定 handler 的 git 动作与 JobResult 形态。"""

    kind: Literal["applied", "target_missing", "mismatch", "route", "rewrite", "scan"]
    error: str = ""
    assignment: int = 0
    fixed_by_take: int = 0


async def execute_restructure_unit(
    llm: Any,
    wiki_dir: str | Path,
    unit: Unit,
    *,
    job_id: str,
    progress: Callable[[str], None],
) -> RestructureExecution:
    """执行一个重组单元：核对 → 路由 → 装配 → 逐页成文 → 落盘 → 质量扫描。"""
    progress("页面核对")
    try:
        plan = await prepare_unit(wiki_dir, unit, llm)
    except UnitMismatchError as exc:
        # 单页改写（A→A）的目标页消失：排队期间被删，按正常结束处理
        if len(unit.in_pages) == 1 and unit.in_pages == unit.out_slugs:
            emit_event("restructure_skipped", unit=str(unit.in_pages), reason="target_missing")
            return RestructureExecution(kind="target_missing")
        emit_event("restructure_mismatch", job_id=job_id, detail=str(exc))
        return RestructureExecution(
            kind="mismatch",
            error=f"核对不过（{exc}），声明与盘面不符，请重新分析"[:500],
        )
    except RouteError as exc:
        emit_event("restructure_reverted", job_id=job_id, reason="route", detail=str(exc))
        return RestructureExecution(kind="route", error=f"章节分配失败: {exc}"[:500])

    progress("逐页成文")
    contents: dict[str, str] = {}
    failures: list[str] = []
    for page in unit.out:
        try:
            text = await rewrite_unit_page(
                llm,
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
        emit_event(
            "restructure_reverted",
            job_id=job_id,
            reason="rewrite",
            detail="; ".join(failures)[:300],
        )
        return RestructureExecution(kind="rewrite", error="; ".join(failures)[:500])

    progress("写入落盘")
    apply_unit(wiki_dir, unit, plan, contents)

    progress("质量扫描")
    errors = [issue for issue in scan_wiki(Path(wiki_dir)) if issue.level == "error"]
    if errors:
        reason = "; ".join(str(issue) for issue in errors[:3])
        emit_event(
            "restructure_reverted", job_id=job_id, reason="scan_gate", detail=reason[:300]
        )
        logger.error("  扫描闸门不过，单元撤销: %s", reason)
        return RestructureExecution(kind="scan", error=f"扫描闸门不过: {reason}"[:500])

    return RestructureExecution(
        kind="applied", assignment=len(plan.assignment), fixed_by_take=len(plan.fixed)
    )
