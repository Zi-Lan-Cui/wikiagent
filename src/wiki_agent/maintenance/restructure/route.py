"""运行时章节分配（路由）：执行时按当前 wiki 状态计算，不进 payload。

分配表只回答"每个输入章节进哪个输出页"；显式 take 的输出页由代码直接
生成归属，不请求 LLM。分配完整性在此校验：漏配、重配、空 out、
非法目标都会使单元失败。
"""

from __future__ import annotations

from typing import Any

from wiki_agent.compiler.checked_call import invoke_checked
from wiki_agent.compiler.models import JSON_MODE, NO_THINKING
from wiki_agent.conversation import Message
from wiki_agent.wiki.sections import Section

from . import prompts
from .models import RouteError, Unit


def take_assignment(unit: Unit, sections: list[Section]) -> dict[str, str]:
    """take 声明 → 固定归属。章节必须存在于 in 页，且不得被两个输出页重复取用。

    未被 take 覆盖的章节归属其余待分配页，由路由 LLM 决定；"take 未覆盖
    且无待分配页"的情况不在此预判，统一由 route_unit 尾部的完整性检查
    拒绝（只在单一位置判定）。
    """
    assignment: dict[str, str] = {}
    for page in unit.out:
        for t in page.take:
            matched = [
                s for s in sections
                if s.slug == t.from_slug and (not t.sections or s.heading in t.sections)
            ]
            if not matched:
                raise RouteError(f"take 未命中章节: {t.from_slug}::{t.sections}")
            for s in matched:
                if s.id in assignment:
                    raise RouteError(f"章节被两个输出页重复取用: {s.id}")
                assignment[s.id] = page.slug
    return assignment


async def route_unit(
    llm: Any, unit: Unit, sections: list[Section]
) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (分配表：章节 id→out_slug, 固定表：take 指定的部分，供审计)。

    全部输出页都有 take 时不调 LLM。删除单元（out 空）直接短路：
    没有输出页，不存在分配问题。
    """
    if not unit.out:
        return {}, {}
    fixed = take_assignment(unit, sections)
    flexible_pages = [p for p in unit.out if not p.take]
    remaining = [s for s in sections if s.id not in fixed]
    assignment = dict(fixed)

    if flexible_pages and remaining:
        response = await invoke_checked(
            llm,
            action="路由",
            error=RouteError,
            messages=[
                Message(role="system", content=prompts.ROUTE_SYSTEM),
                Message(role="user", content=prompts.route_user(
                    unit, remaining,
                    "已固定归属的章节不在下面清单里，只分配列出的章节。"
                    if fixed else "",
                )),
            ],
            max_tokens=4096,
            check=prompts.check_route_json,
            extra_body=NO_THINKING,
            max_attempts=2,
            response_format=JSON_MODE,
        )
        allowed = {p.slug for p in flexible_pages}
        for item in prompts.json_of(response.content)["assign"]:
            sid, to = str(item["section"]), str(item["to"])
            if sid not in {s.id for s in remaining}:
                raise RouteError(f"路由返回未知章节: {sid}")
            if to not in allowed:
                raise RouteError(f"路由目标非法: {to} 不在待分配输出页中")
            if sid in assignment:
                raise RouteError(f"章节被重复分配: {sid}")
            assignment[sid] = to

    # 完整性：每个章节恰好一次；每个输出页至少一章（删除单元 out 为空，跳过）
    all_ids = {s.id for s in sections}
    missing = sorted(all_ids - set(assignment))
    if missing:
        raise RouteError(f"章节无处安放（禁止丢弃）: {missing[:5]}")
    extra = sorted(set(assignment) - all_ids)
    if extra:
        raise RouteError(f"分配表含幽灵章节: {extra[:5]}")
    for page in unit.out:
        if not any(to == page.slug for to in assignment.values()):
            raise RouteError(f"输出页没有任何章节: {page.slug}")
    return assignment, fixed
