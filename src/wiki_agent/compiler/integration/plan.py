"""Plan 阶段——集成决策：策展人（new/update 开放决策）。"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.checked_call import invoke_checked
from wiki_agent.compiler.integration.checks import (
    VALID_DISPOSITIONS,
    check_plan_json,
)
from wiki_agent.compiler.integration.common import load_valid_slugs
from wiki_agent.compiler.integration.parse import parse_plan
from wiki_agent.compiler.models import (
    JSON_MODE,
    NO_THINKING,
    AnalysisResult,
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
)
from wiki_agent.conversation import Message
from wiki_agent.errors import IngestStage
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import emit_event, get_logger

logger = get_logger("STAGES")

_PLAN_TOKENS = 8_192


class Planner:
    """plan 阶段接口。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def _invoke_plan(
        self,
        extract: ExtractResult,
        system_prompt: str,
        user_prompt: str = "",
    ) -> IntegrationPlan:
        """共享的 LLM 调用 + 校验 + 解析 + 决策追踪。

        system/user 双消息（prompt cache 拆分）——固定角色留 system，
        动态数据（分析文本/index/当前页）放 user。

        Args:
            extract: 源文档抽取结果。
            system_prompt: plan system prompt。
            user_prompt: plan user prompt。

        Returns:
            解析后的 IntegrationPlan（含原始输出 raw）。

        Raises:
            IngestError: 校验穷尽后仍失败。
        """
        allowed = getattr(self._prompts, "ALLOWED_DISPOSITIONS", VALID_DISPOSITIONS)

        def check_plan(content: str) -> tuple[bool, str]:
            return check_plan_json(content, allowed_dispositions=allowed)

        response = await invoke_checked(
            self._llm,
            stage=IngestStage.PLAN,
            action="plan",
            source=extract.source_identity,
            messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt),
            ],
            max_tokens=_PLAN_TOKENS,
            check=check_plan,
            extra_body=NO_THINKING,
            max_attempts=2,
            response_format=JSON_MODE,
        )
        raw = response.content
        plan = parse_plan(raw)
        plan.raw = raw
        return plan

    async def plan(
        self,
        extract: ExtractResult,
        analysis: AnalysisResult,
        *,
        schema: str = "",
        purpose: str = "",
        index_content: str = "",
    ) -> IntegrationPlan:
        """制定集成计划（子类实现）。

        Args:
            extract: 源文档抽取结果。
            analysis: analyze 阶段的分析结果。
            schema: 目录规范文本。
            purpose: 知识库使命文本。
            index_content: index.md 全文。

        Returns:
            集成计划（页面目标列表）。

        Raises:
            NotImplementedError: 未实现。
        """
        raise NotImplementedError


class CuratorPlanner(Planner):
    """策展人——new/update 开放决策。"""

    async def plan(
        self,
        extract: ExtractResult,
        analysis: AnalysisResult,
        *,
        schema: str = "",
        purpose: str = "",
        index_content: str = "",
    ) -> IntegrationPlan:
        """策展人决策——new/update 开放决策。

        Args:
            extract: 源文档抽取结果。
            analysis: analyze 阶段的分析结果。
            schema: 目录规范文本。
            purpose: 知识库使命文本。
            index_content: index.md 全文。

        Returns:
            集成计划；引用会过滤无效 slug 并 emit plan_decision 事件。
        """
        plan = await self._invoke_plan(
            extract,
            self._prompts.plan_system(schema=schema, purpose=purpose),
            self._prompts.plan_user(
                extract,
                _format_analysis_for_plan(analysis),
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
        emit_event(
            "plan_decision",
            file=extract.source_identity,
            targets=[
                {
                    "path": t.wiki_path,
                    "disposition": t.disposition.value,
                    "page_type": t.page_type,
                    "reason": t.reason,
                    "references": t.references,
                }
                for t in plan.page_targets
            ],
            analysis_rels=[
                {"from": r.from_page, "to": r.to_page, "relation": r.relation}
                for r in analysis.relationships
            ],
        )
        return plan


def _format_analysis_for_plan(analysis: AnalysisResult) -> str:
    """将 AnalysisResult 格式化为 plan prompt 可用的文本。

    自由分析是核心依据（原样传递），结构化尾巴作索引——
    plan 从自由文本里读推理，从尾巴里查实体/关系。

    Args:
        analysis: analyze 阶段结果。

    Returns:
        格式化文本；无结构化内容时回退到原始分析文本。
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
            parts.append(
                f"- {r.from_page} → {r.to_page}: {r.relation}{' — ' + r.detail if r.detail else ''}"
            )
        parts.append("")

    return "\n".join(parts) if parts else analysis.raw_analysis


def filter_plan_refs(
    targets: list[PageTarget],
    valid_slugs: set[str],
) -> list[PageTarget]:
    """过滤每个 PageTarget.references 中的无效 slug。

    slug 不在 valid_slugs 中的引用会被移除并记录警告。

    Args:
        targets: 页面目标列表（就地修改 references）。
        valid_slugs: 合法 slug 集合。

    Returns:
        过滤后的 targets（原列表）。
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
                    slug,
                    r.get("slug", ""),
                )
        removed = len(t.references) - len(valid_refs)
        if removed:
            logger.info(
                "  plan 引用过滤 [%s]: %d/%d 保留", t.wiki_path, len(valid_refs), len(t.references)
            )
        t.references = valid_refs
    return targets
