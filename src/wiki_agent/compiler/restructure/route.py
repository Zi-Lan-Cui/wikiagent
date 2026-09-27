"""运行时章节分配（路由）：执行时以当前盘面计算，不进 payload。

分配表只回答"每个输入章节进哪个输出页"；显式 take 的输出页由代码直接
生成归属、不请求 LLM。守恒在这里成为可校验约束：漏配、重配、空 out、
非法目标都使单元失败。
"""

from __future__ import annotations

from typing import Any

from wiki_agent.compiler.models import JSON_MODE, NO_THINKING
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry

from . import prompts
from .models import RouteError, Unit
from .sections import Section


def take_assignment(unit: Unit, sections: list[Section]) -> dict[str, str]:
    """take 声明 → 固定归属。章节必须存在于 in 页；未被 take 指定的
    章节归属其余待分配页（若无其他 out，则视为删除式裁剪的剩余内容，
    仍必须落入某个 out——守恒不因 take 而失效）。"""
    by_id = {s.id: s for s in sections}
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
    unassigned = [sid for sid in by_id if sid not in assignment]
    flexible = [p.slug for p in unit.out if not p.take]
    if unassigned:
        if not flexible:
            raise RouteError(f"take 未覆盖且无待分配页，章节无处安放: {unassigned[:5]}")
        # 剩余章节默认全部进第一个待分配页——路由 LLM 会对这部分改写归属
        pass
    return assignment


async def route_unit(
    llm: Any, unit: Unit, sections: list[Section]
) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (分配表 章节id→out_slug, 固定表——take 直给的部分，供审计)。

    全单元都是 take 装配时不调 LLM。删除单元（out 空）短路：章节的
    归宿就是消失，没有分配可算——守恒约束不适用于无输出页的单元。
    """
    if not unit.out:
        return {}, {}
    fixed = take_assignment(unit, sections)
    flexible_pages = [p for p in unit.out if not p.take]
    remaining = [s for s in sections if s.id not in fixed]
    assignment = dict(fixed)

    if flexible_pages and remaining:
        response = await async_invoke_with_retry(
            llm,
            [
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
        if not response.check_ok:
            raise RouteError(f"路由校验失败: {response.check_reason}")
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

    # 守恒：每个章节恰好一次；每个输出页至少一章（删除单元 out 为空，跳过）
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
