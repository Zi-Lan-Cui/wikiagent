"""结构重组的确定性冲突与依赖消解（无 LLM）——复判之后、确认之前。

LLM 输出独立提议互不知情，冲突由代码归一化；无法确定性消解的收集为
Conflict 交 review.re_arbitrate 或人工。
"""

from __future__ import annotations

from wiki_agent.compiler.restructure.models import Conflict, Proposal
from wiki_agent.log import emit_event, get_logger

logger = get_logger("RESTRUCTURE")


def _page_quality(page: dict) -> int:
    """页面质量分——冲突时定 merge 方向。

    Args:
        page: 页面数据。

    Returns:
        质量分（正文长度 + 2×摘要长度）。
    """
    return len(page["body"]) + 2 * len(page["summary"])


def _src_of(prop: Proposal) -> str:
    """返回 merge 的被吸收页。

    merge_into_first 吸收 pages[1]，反之 pages[0]。

    Args:
        prop: merge 提议。

    Returns:
        被吸收页 slug。
    """
    return prop.pages[1] if prop.op == "merge_into_first" else prop.pages[0]


def resolve_conflicts(
    proposals: list[Proposal],
    pages: dict[str, dict],
) -> tuple[list[Proposal], list[Conflict]]:
    """提议间一致性分析——确定性消解 + 冲突收集。

    消解规则（按信息优先排序）:
    1. 完全重复的提议 → 去重
    2. 同一对页面的双向 merge → 质量高的一页做吸收方，另一条丢弃
    3. delete 被吸收页 → merge 赢（merge 保留信息，delete 只减不增）
    4. delete 吸收方 → Conflict（吸收方即将消失，LLM 复裁）
    5. merge 链（A→B 且 B→C）→ 合法保留（每步原子，内容沿链流动）

    Args:
        proposals: 复判后的提议。
        pages: 页面表（质量分用）。

    Returns:
        (clean, conflicts)——clean 直接进执行，conflicts 待复裁。
    """
    # 1. 去重；create/trim 的 sections 也属于操作身份，不能误合并。
    seen: set[tuple] = set()
    unique: list[Proposal] = []
    for p in proposals:
        key = (p.op, tuple(sorted(p.pages)), p.target, tuple(p.sections))
        if key in seen:
            logger.info("  去重: %s", p.pages)
            continue
        seen.add(key)
        unique.append(p)

    # 索引: merge 按无序对，delete 按单页
    merges: dict[tuple, list[Proposal]] = {}
    deletes: dict[str, Proposal] = {}
    for p in unique:
        if p.op.startswith("merge"):
            merges.setdefault(tuple(sorted(p.pages)), []).append(p)
        elif p.op == "delete":
            deletes[p.pages[0]] = p

    clean: list[Proposal] = []
    conflicts: list[Conflict] = []

    # 2. 双向 merge → 质量定方向
    for pair, group in merges.items():
        if len(group) == 1:
            clean.append(group[0])
            continue
        best = max(group, key=lambda p: _page_quality(pages[p.target]))
        logger.info(
            "  方向互斥消解: %s ↔ %s → 吸收方 %s（质量分定）", pair[0], pair[1], best.target
        )
        clean.append(best)

    # 3/4. delete vs merge 撞页
    for m in clean:
        if not m.op.startswith("merge"):
            continue
        src = _src_of(m)
        # 3. delete 被吸收页 → merge 赢
        if src in deletes:
            logger.info("  merge 覆盖 delete: %s（将被 %s 吸收）", src, m.target)
            del deletes[src]
        # 4. delete 吸收方 → 冲突
        if m.target in deletes:
            conflicts.append(
                Conflict(
                    kind="delete_vs_merge",
                    proposals=[m, deletes[m.target]],
                    detail=f"{m.target} 既是 merge 吸收方又被提议删除",
                )
            )

    clean.extend(deletes.values())

    # create/trim 不参与 merge/delete 的语义冲突，但必须保留，并在落盘前
    # 做一次依赖和目标校验。拓扑排序在 execute 中再次执行，防止调用方
    # 传入的列表顺序改变语义。
    clean.extend(p for p in unique if p.op in ("create", "trim"))
    clean, dependency_conflicts = _validate_operation_sequence(clean, pages)
    conflicts.extend(dependency_conflicts)
    if conflicts:
        emit_event("restructure_conflict", count=len(conflicts), kinds=[c.kind for c in conflicts])
    return clean, conflicts


def _validate_operation_sequence(
    proposals: list[Proposal],
    pages: dict[str, dict],
) -> tuple[list[Proposal], list[Conflict]]:
    """校验原子操作的 id/依赖/目标，并按依赖排序。

    这里不推断章节内容，也不替 LLM 修正路径；不满足结构契约的操作
    进入冲突结果，避免执行阶段出现半拆分。
    """
    conflicts: list[Conflict] = []
    ids: dict[str, Proposal] = {}
    normalized: list[Proposal] = []
    for i, prop in enumerate(proposals, 1):
        op_id = prop.id or f"op_{i}"
        if op_id in ids:
            conflicts.append(
                Conflict("duplicate_operation_id", [prop, ids[op_id]], f"重复操作 id: {op_id}")
            )
            continue
        fixed = Proposal(
            op=prop.op,
            pages=prop.pages,
            target=prop.target,
            reason=prop.reason,
            id=op_id,
            group_id=prop.group_id,
            depends_on=list(prop.depends_on),
            sections=list(prop.sections),
            title=prop.title,
            summary=prop.summary,
            goal=prop.goal,
        )
        if prop.op == "create":
            if len(prop.pages) != 1 or not prop.target or prop.target in pages:
                conflicts.append(
                    Conflict("invalid_create", [prop], f"create 目标无效或已存在: {prop.target}")
                )
                continue
        if prop.op == "trim" and (len(prop.pages) != 1 or not prop.sections):
            conflicts.append(Conflict("invalid_trim", [prop], "trim 缺少源页或章节"))
            continue
        ids[op_id] = fixed
        normalized.append(fixed)

    valid: list[Proposal] = []
    for prop in normalized:
        missing = [dep for dep in prop.depends_on if dep not in ids]
        if missing:
            conflicts.append(Conflict("missing_dependency", [prop], f"依赖不存在: {missing}"))
            continue
        valid.append(prop)

    # Kahn 拓扑排序；环和被环阻塞的操作都不进入执行。
    by_id = {p.id: p for p in valid}
    indegree = {p.id: sum(d in by_id for d in p.depends_on) for p in valid}
    ready = [p.id for p in valid if indegree[p.id] == 0]
    ordered: list[Proposal] = []
    while ready:
        current = ready.pop(0)
        ordered.append(by_id[current])
        for p in valid:
            if current in p.depends_on:
                indegree[p.id] -= 1
                if indegree[p.id] == 0:
                    ready.append(p.id)
    if len(ordered) != len(valid):
        cyclic = [p for p in valid if p not in ordered]
        conflicts.append(Conflict("cyclic_dependency", cyclic, "原子操作依赖存在环"))
    return ordered, conflicts
