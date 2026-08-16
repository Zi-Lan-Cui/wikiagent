"""编译四阶段——Search/Analyze/Plan/Execute 拆为独立类，按模式组装。

设计（取代 integrate.py 的单体 Integrator）:
- Searcher / Analyzer / Executor 单一实现（模式间零差异）
- Planner 接口 + 两个实现:
    CuratorPlanner 策展人——compile 用（new/update 开放决策）
    PolisherPlanner 润色师——refine 用（只更新自己；current_page 是它
    自己的构造语义，不泄漏到共享接口）
- Integrator 是组装器（facade）——保持 pipeline 的编排形状不变，
  模式差异收敛在"组装哪个 Planner"
- prompts 模块保留（prompt 文本是独立资产，模块级继承已足够）

模式知识分布:
    CuratorPlanner 不知道 current_page 存在
    PolisherPlanner 自己从 wiki_dir 读本页 frontmatter（goal/gaps/summary）
    组装工厂 compile_integrator()/refine_integrator() 是唯一知道组合的地方
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path

from wiki_agent.compiler.checks import (
    _check_analyze_json,
    _check_json_array,
    _check_page_output,
    _check_plan_json,
    _VALID_DISPOSITIONS,
)
from wiki_agent.compiler.parse import (
    _extract_headings,
    _normalize_wiki_path,
    _parse_analysis,
    split_frontmatter,
    _parse_plan,
    _parse_search_result,
)
from wiki_agent.compiler.models import (
    AnalysisResult,
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
    SearchResult,
)
from wiki_agent.compiler.normalize import extract_related, fix_wikilinks, normalize_page
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.llm.llm import LLMClient
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import emit_event, get_logger
from wiki_agent.message import Message

logger = get_logger("STAGES")

_SEARCH_TOKENS = 2_048
_ANALYZE_TOKENS = 6_000
_PLAN_TOKENS = 8_192
_UPDATE_TOKENS = 8_000
# 页面生成总尝试次数（retry 层语义: 总尝试，原 1 = 零重试）
_PAGE_GEN_RETRIES = 2
_NO_THINKING = {"thinking": {"type": "disabled"}}


def load_valid_slugs(wiki_dir: str | Path) -> set[str]:
    """从 wiki/index.md 读取已有页面 slug 集合。"""
    try:
        return extract_slugs_from_index(
            (Path(wiki_dir) / "index.md").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()


# ════════════════════════════════════════════════════════════
#  Searcher——L1 索引初筛
# ════════════════════════════════════════════════════════════

class Searcher:
    """search 阶段: LLM 从 index 选候选页面。模式间零差异。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def search(self, extract: ExtractResult, index_content: str) -> SearchResult:
        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=self._prompts.search_system()),
                Message(role="user", content=self._prompts.search_user(extract, index_content)),
            ],
            max_tokens=_SEARCH_TOKENS,
            check=_check_json_array,
            extra_body=_NO_THINKING,
            max_retries=2,
        )
        # 校验穷尽后仍失败 → 显式 raise，不许静默降级成"0 候选"。
        if not response.check_ok:
            raise IngestError(
                IngestStage.SEARCH,
                f"search 输出校验失败（重试后仍失败）: {response.check_reason}",
                source=extract.source_identity,
                raw=response.content,
            )
        paths = _parse_search_result(response.content)
        logger.info("  search: %d 个候选页面", len(paths))
        return SearchResult(rel_paths=paths)


# ════════════════════════════════════════════════════════════
#  Analyzer——关系分析
# ════════════════════════════════════════════════════════════

class Analyzer:
    """analyze 阶段: 文档与候选页的两段式关系分析。模式间零差异。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def _read_page(self, wiki_path: str) -> str:
        try:
            return (self._wiki_dir / wiki_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def analyze(
        self, extract: ExtractResult, result: SearchResult,
    ) -> AnalysisResult:
        """② 关系分析: 新文档与 search 候选页面之间的关系。"""
        if not result.rel_paths:
            return AnalysisResult(source_identity=extract.source_identity)

        outlines: list[str] = []
        for path in result.rel_paths:
            content = await self._read_page(path)
            fm = split_frontmatter(content)[0] if content else {}
            title = fm.get("title", "")
            summary = fm.get("summary", "")
            page_type = fm.get("type", "")
            sources = fm.get("sources", "")
            related = fm.get("related", "")
            gaps = fm.get("gaps", "")
            goal = fm.get("goal", "")
            headings = _extract_headings(content)
            slug = path.replace("wiki/", "").replace(".md", "")
            meta = f"- [[{slug}]] — [{page_type}] {path} — {title}"
            if summary:
                meta += f" — {summary}"
            if goal:
                meta += f" — 使命: {goal}"
            if gaps:
                meta += f" — 缺口声明: {gaps}"
            if sources:
                meta += f" — 来源: {sources}"
            if related:
                meta += f" — 已有引用: {related}"
            if headings:
                meta += f"\n{headings}"
            outlines.append(meta)

        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=self._prompts.analyze_system()),
                Message(role="user", content=self._prompts.analyze_user(
                    extract, "\n\n".join(outlines),
                )),
            ],
            max_tokens=_ANALYZE_TOKENS,
            check=lambda content: _check_analyze_json(
                content, candidates=result.rel_paths,
                # 当前文档真实 slug 并入合法集——LLM 用真实路径自称
                # 比固定标识 current-doc 自然（实测 refine 高频违规）
                extra_refs={extract.source_identity},
            ),
            extra_body=_NO_THINKING,
            max_retries=2,
        )
        raw = response.content
        # 空响应/校验不过由 retry 层处理——这里只做穷尽后的显式报告。
        if not response.check_ok:
            raise IngestError(
                IngestStage.ANALYZE,
                f"analyze 输出校验失败（重试后仍失败）: {response.check_reason}",
                source=extract.source_identity,
                raw=raw,
            )
        analysis = _parse_analysis(raw, extract.source_identity)
        logger.info("  analysis: %d entities, %d concepts, %d relationships, "
                    "自由分析 %d chars",
                    len(analysis.entities), len(analysis.concepts),
                    len(analysis.relationships), len(analysis.analysis_text))
        return analysis


# ════════════════════════════════════════════════════════════
#  Planner——决策（接口 + 两个实现）
# ════════════════════════════════════════════════════════════

class Planner:
    """plan 阶段接口——共享签名，实现自带模式语义。

    needs_current_page: 组装器据此决定是否传 current_page。
    """

    needs_current_page: bool = False

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def _invoke_plan(
        self, extract: ExtractResult, system_prompt: str, user_prompt: str = "",
    ) -> IntegrationPlan:
        """共享的 LLM 调用 + 校验 + 解析 + 决策追踪。

        system/user 双消息（prompt cache 拆分）——固定角色留 system，
        动态数据（分析文本/index/当前页）放 user。
        """
        allowed = getattr(self._prompts, "ALLOWED_DISPOSITIONS", _VALID_DISPOSITIONS)
        check_plan = lambda content: _check_plan_json(
            content, allowed_dispositions=allowed)

        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt),
            ],
            max_tokens=_PLAN_TOKENS,
            check=check_plan,
            extra_body=_NO_THINKING,
            max_retries=2,
        )
        raw = response.content
        if not response.check_ok:
            raise IngestError(
                IngestStage.PLAN,
                f"plan 输出校验失败: {response.check_reason}",
                source=extract.source_identity,
                raw=raw,
            )
        plan = _parse_plan(raw)
        plan.raw = raw
        return plan

    async def plan(
        self, extract: ExtractResult, analysis: AnalysisResult, *,
        schema: str = "", purpose: str = "", index_content: str = "",
    ) -> IntegrationPlan:
        raise NotImplementedError


class CuratorPlanner(Planner):
    """策展人——compile 模式: new/update 开放决策。不知道 current_page 存在。"""

    async def plan(
        self, extract: ExtractResult, analysis: AnalysisResult, *,
        schema: str = "", purpose: str = "", index_content: str = "",
    ) -> IntegrationPlan:
        plan = await self._invoke_plan(
            extract,
            self._prompts.plan_system(schema=schema, purpose=purpose),
            self._prompts.plan_user(
                extract, _format_analysis_for_plan(analysis),
                index_content=index_content,
            ),
        )
        # 后处理: 过滤 references 中不存在的 slug。
        # 有效集合 = 已有页面 + 本次 plan 新建的页面。
        valid_slugs = load_valid_slugs(self._wiki_dir)
        for t in plan.page_targets:
            if t.disposition == Disposition.NEW:
                valid_slugs.add(t.wiki_path.replace(".md", ""))
        if valid_slugs:
            filter_plan_refs(plan.page_targets, valid_slugs)

        # 决策追踪——结果 + 依据（关系分析）同一条事件
        emit_event("plan_decision",
                   file=extract.source_identity,
                   targets=[
                       {"path": t.wiki_path,
                        "disposition": t.disposition.value,
                        "reason": t.reason,
                        "references": t.references}
                       for t in plan.page_targets
                   ],
                   analysis_rels=[
                       {"from": r.from_page, "to": r.to_page,
                        "relation": r.relation}
                       for r in analysis.relationships
                   ])
        return plan


class PolisherPlanner(Planner):
    """润色师——refine 模式: 只更新自己。

    current_page 是本类的领域参数（compile 的 Planner 不知道它存在）。
    本页 goal/gaps/summary 自己从 wiki_dir 读——目标完成度判断依据。
    """

    needs_current_page: bool = True

    async def plan(
        self, extract: ExtractResult, analysis: AnalysisResult, *,
        schema: str = "", purpose: str = "", index_content: str = "",
        current_page: str = "",
    ) -> IntegrationPlan:
        page_meta = self._page_meta(current_page)
        plan = await self._invoke_plan(
            extract,
            self._prompts.plan_system(schema=schema, purpose=purpose),
            self._prompts.plan_user(
                extract, _format_analysis_for_plan(analysis),
                current_page=current_page, page_meta=page_meta,
            ),
        )
        # 只拿不放的代码兜底——只保留 update 且指向自身的 target
        self._filter_self_updates(plan, current_page)
        emit_event("plan_decision",
                   file=extract.source_identity,
                   mode="refine",
                   targets=[
                       {"path": t.wiki_path,
                        "disposition": t.disposition.value,
                        "reason": t.reason}
                       for t in plan.page_targets
                   ],
                   analysis_rels=[
                       {"from": r.from_page, "to": r.to_page,
                        "relation": r.relation}
                       for r in analysis.relationships
                   ])
        return plan

    def _page_meta(self, current_page: str) -> str:
        """读本页 frontmatter 摘要——goal/gaps/summary/type。"""
        if not current_page:
            return ""
        try:
            content = (self._wiki_dir / f"{current_page}.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        fm = split_frontmatter(content)[0]
        return "\n".join(
            f"- {k}: {fm[k]}"
            for k in ("goal", "gaps", "summary", "type")
            if fm.get(k)
        )

    def _filter_self_updates(self, plan: IntegrationPlan, self_slug: str) -> None:
        """丢弃非 self-update 的 target——prompt+校验之后最后一道。"""
        kept = []
        for t in plan.page_targets:
            t_slug = _normalize_wiki_path(t.wiki_path).replace(".md", "")
            if t.disposition != Disposition.UPDATE:
                logger.warning("  refine 丢弃非 update target: %s", t.wiki_path)
                continue
            if t_slug != self_slug:
                logger.warning("  refine 丢弃非自身 target: %s（自身 %s）",
                               t.wiki_path, self_slug)
                continue
            kept.append(t)
        plan.page_targets = kept
        if kept:
            logger.info("  refine 保留 self-update: %d", len(kept))


# ════════════════════════════════════════════════════════════
#  Executor——落盘执行
# ════════════════════════════════════════════════════════════

class Executor:
    """execute 阶段: 并行生成/更新页面 + 失败隔离 + 死链兜底。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def _read_page(self, wiki_path: str) -> str:
        try:
            return (self._wiki_dir / wiki_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def _write_page(self, wiki_path: str, content: str) -> None:
        """落盘——内容处理链在 execute 内完成，这里只写文件。"""
        wiki_path = _normalize_wiki_path(wiki_path)
        full = self._wiki_dir / wiki_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    async def _generate_page(
        self, target: PageTarget, existing: str, extract: ExtractResult,
    ) -> str:
        """按 disposition 生成页面——new 从零生成 / update 合并已有页。

        校验不过 → 重试；穷尽后仍不过 → raise（质量闸门，不许静默落盘）。
        """
        if not existing:
            system_prompt = self._prompts.new_page_system()
            user_prompt = self._prompts.new_page_user(target, extract)
        else:
            system_prompt = self._prompts.update_system()
            user_prompt = self._prompts.update_user(target, existing, extract)
        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt),
            ],
            max_tokens=_UPDATE_TOKENS,
            check=_check_page_output,
            extra_body=_NO_THINKING,
            max_retries=_PAGE_GEN_RETRIES,
        )
        if not response.check_ok:
            raise IngestError(
                IngestStage.EXECUTE,
                f"页面生成校验失败（重试后仍失败）: {response.check_reason}",
                source=target.wiki_path,
                raw=response.content,
            )
        return response.content

    async def execute(
        self, plan: IntegrationPlan, extract: ExtractResult,
    ) -> list[PageTarget]:
        """执行 plan——并行处理每个 target，失败隔离 + 死链兜底。"""
        if not plan.page_targets:
            return []

        today = date.today().isoformat()
        source = extract.source_identity

        # 组装有效 slug 集合：已有 + 本次要创建的新页
        valid_slugs = load_valid_slugs(self._wiki_dir)
        for t in plan.page_targets:
            if t.disposition == Disposition.NEW:
                valid_slugs.add(t.wiki_path.replace(".md", ""))

        failed_paths: set[str] = set()

        async def _execute_target(target: PageTarget) -> PageTarget | None:
            try:
                existing = await self._read_page(target.wiki_path)
                existing_fm = split_frontmatter(existing)[0] if existing else None
                raw_content = await self._generate_page(target, existing, extract)

                content, issues = normalize_page(
                    raw_content,
                    path=target.wiki_path,
                    valid_slugs=valid_slugs,
                    source_identity=source,
                    today=today,
                    existing=existing_fm,
                )
                for issue in issues:
                    if issue.level == "error":
                        raise ValueError(f"页面质量不合格: {issue}")

                await self._write_page(target.wiki_path, content)
                logger.info("  ✓ %s", target.wiki_path)
                return target
            except Exception as exc:
                failed_paths.add(_normalize_wiki_path(target.wiki_path))
                logger.error("  ✗ %s 生成失败: %s", target.wiki_path, str(exc)[:200])
                emit_event("page_generation", path=target.wiki_path, status="error",
                           error=str(exc))
                # 失败返回 None——不能 return target（update 目标旧页仍存在，
                # 会骗过调用方 exists() 过滤，把失败算成成功）
                return None

        results = await asyncio.gather(*[_execute_target(t) for t in plan.page_targets])
        results = [r for r in results if r is not None]

        # 兜底死链清理: 仅当有页面生成失败时触发
        if failed_paths:
            actual_slugs = load_valid_slugs(self._wiki_dir)
            actual_slugs.update(
                t.wiki_path.replace(".md", "")
                for t in plan.page_targets
                if (self._wiki_dir / _normalize_wiki_path(t.wiki_path)).exists()
            )
            for t in plan.page_targets:
                full = self._wiki_dir / _normalize_wiki_path(t.wiki_path)
                if not full.exists():
                    continue
                content = full.read_text(encoding="utf-8")
                fixed = fix_wikilinks(content, valid_slugs=actual_slugs)
                fixed = extract_related(fixed, valid_slugs=actual_slugs)
                if fixed != content:
                    full.write_text(fixed, encoding="utf-8")
                    logger.info("  死链清理: %s 已更新（引用了生成失败的页面）", t.wiki_path)

        return results


# ════════════════════════════════════════════════════════════
#  编译领域工具
# ════════════════════════════════════════════════════════════

def _format_analysis_for_plan(analysis: AnalysisResult) -> str:
    """将 AnalysisResult 格式化为 plan prompt 可用的文本。

    自由分析是核心依据（原样传递），结构化尾巴作索引——
    plan 从自由文本里读推理，从尾巴里查实体/关系。
    """
    parts: list[str] = []

    # 自由分析主体优先——plan 的决策依据
    if analysis.analysis_text:
        parts.append(analysis.analysis_text)
        parts.append("")

    # 结构化尾巴作补充索引
    if analysis.entities:
        parts.append("## 提取到的命名实体")
        for e in analysis.entities:
            imp = f" [{e.get('importance', '')}]" if e.get("importance") else ""
            parts.append(f"- {e['name']} ({e.get('type', '')}){imp} — {e.get('description', '')}")
        parts.append("")

    if analysis.concepts:
        parts.append("## 提取到的抽象概念")
        for c in analysis.concepts:
            imp = f" [{c.get('importance', '')}]" if c.get("importance") else ""
            parts.append(f"- {c['name']}{imp} — {c.get('description', '')}")
        parts.append("")

    if analysis.relationships:
        parts.append("## 与已有页面的关系判定（from → to 表示信息流动方向）")
        for r in analysis.relationships:
            parts.append(f"- {r.from_page} → {r.to_page}: {r.relation}"
                         f"{' — ' + r.detail if r.detail else ''}")
        parts.append("")

    return "\n".join(parts) if parts else analysis.raw_analysis


def extract_slugs_from_index(index_content: str) -> set[str]:
    """从 wiki/index.md 提取所有已有页面 slug。

    匹配 `[[entities/xxx]]`、`[[concepts/xxx]]`、`[[topics/xxx]]` 格式。
    """
    slugs: set[str] = set()
    for m in re.finditer(r"\[\[([a-zA-Z0-9][^\]]+?)\]\]", index_content):
        slug = m.group(1).strip()
        # 只收集 entities/、concepts/、topics/ 下的 slug，不含 .md
        if re.match(r"^(entities|concepts|topics)/", slug):
            slugs.add(slug.replace(".md", ""))
    return slugs


def filter_plan_refs(
    targets: list[PageTarget], valid_slugs: set[str],
) -> list[PageTarget]:
    """过滤每个 PageTarget.references 中的无效 slug。

    slug 不在 valid_slugs 中的引用会被移除并记录警告。
    """
    for t in targets:
        if not t.references or not valid_slugs:
            continue
        valid_refs = []
        for r in t.references:
            slug = r.get("slug", "").replace(".md", "")
            if slug in valid_slugs:
                valid_refs.append(r)
            else:
                logger.warning(
                    "  ✗ plan 引用无效（slug='%s'）: [[%s]] — 已过滤",
                    slug, r.get("slug", ""),
                )
        removed = len(t.references) - len(valid_refs)
        if removed:
            logger.info("  plan 引用过滤 [%s]: %d/%d 保留",
                         t.wiki_path, len(valid_refs), len(t.references))
        t.references = valid_refs
    return targets
