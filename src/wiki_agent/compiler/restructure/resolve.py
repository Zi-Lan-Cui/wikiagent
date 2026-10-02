"""冲突消解（提交侧，纯代码）：保证一页只进一个单元。

规则按序检查，违例单元整体丢弃并附理由；代码不做部分合并或拆分，
结构取舍由提议者决定。执行前会按同一规则再检查一次，
防止 wiki 状态已被先前的批次改变。
"""

from __future__ import annotations

from wiki_agent.wiki.pages import PAGE_TYPE_BY_DIR

from .models import Unit, UnitError


def validate_unit(unit: Unit, existing: set[str]) -> str:
    """单元声明合法性检查。返回空串=通过，否则为丢弃理由。"""
    if not unit.in_pages:
        return "没有输入页面——整理不引入新知识，原料必须来自现有页"
    if len(set(unit.in_pages)) != len(unit.in_pages):
        return "输入页面有重复"
    missing = [s for s in unit.in_pages if s not in existing]
    if missing:
        return f"输入页面不存在: {missing}"
    out_slugs = unit.out_slugs
    if len(set(out_slugs)) != len(out_slugs):
        return "输出页面有重名"
    if out_slugs and not any(p.intent or p.take for p in unit.out):
        return "输出页面既没有写作意图也没有材料来源"
    for p in unit.out:
        if p.slug.split("/", 1)[0] not in PAGE_TYPE_BY_DIR:
            return f"输出页面目录不合法: {p.slug}——只能是 concepts/entities/topics"
        if p.slug not in existing and p.slug in unit.in_pages:
            return f"输出页面 {p.slug} 被当作改写对象，但当前并不存在"
        for t in p.take:
            if t.from_slug not in unit.in_pages:
                return f"材料来源 {t.from_slug} 不在这条建议的输入页面里"
    return ""


def resolve_unit_conflicts(
    units: list[Unit], existing: set[str]
) -> tuple[list[Unit], list[tuple[Unit, str]]]:
    """单元间互斥：一批内任何 slug（in 或 out）只属于一个单元；
    out 页与未被本批消费的现存页同名即冲突，不允许覆盖。"""
    clean: list[Unit] = []
    dropped: list[tuple[Unit, str]] = []
    claimed: dict[str, int] = {}  # slug → 单元序号
    for unit in units:
        reason = validate_unit(unit, existing)
        if reason:
            dropped.append((unit, reason))
            continue
        touched = set(unit.in_pages) | set(unit.out_slugs)
        clash = [s for s in sorted(touched) if s in claimed]
        if clash:
            dropped.append(
                (unit, f"页面 {clash} 已属第 {min(claimed[s] for s in clash)} 条建议——一页同时只归一条")
            )
            continue
        overwritten = [s for s in unit.out_slugs if s in existing and s not in set(unit.in_pages)]
        if overwritten:
            dropped.append(
                (unit, f"输出页面与现有页 {overwritten} 同名，但该页不在输入里——改写它须先列为输入")
            )
            continue
        for s in touched:
            claimed[s] = len(clean)
        clean.append(unit)
    return clean, dropped


def assert_units_valid(units: list[Unit], existing: set[str]) -> None:
    """提交/执行前校验：任何违例直接抛 UnitError。消解阶段本应过滤，
    此处仍违例说明调用方绕过了消解，不静默放行。"""
    clean, dropped = resolve_unit_conflicts(units, existing)
    if dropped:
        raise UnitError("; ".join(f"{u.in_pages}: {r}" for u, r in dropped))
    _ = clean
