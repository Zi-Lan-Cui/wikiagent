"""冲突消解（提交侧，纯代码）：保证一页只进一个单元。

规则按序检查，违例单元整体丢弃并给理由——结构决定权在人，代码不仲裁
谁让步。执行前会再跑一次同规则（盘面可能已被上一批改变时的防御）。
"""

from __future__ import annotations

from .models import Unit, UnitError


def validate_unit(unit: Unit, existing: set[str]) -> str:
    """单源自洽检查，返回空串=通过，否则为丢弃理由。"""
    if not unit.in_pages:
        return "in_pages 为空——维护不引入新知识，原料必须来自现有页"
    if len(set(unit.in_pages)) != len(unit.in_pages):
        return "in_pages 有重复"
    missing = [s for s in unit.in_pages if s not in existing]
    if missing:
        return f"输入页不存在: {missing}"
    out_slugs = unit.out_slugs
    if len(set(out_slugs)) != len(out_slugs):
        return "out 有重复 slug"
    if out_slugs and not any(p.intent or p.take for p in unit.out):
        return "out 页既无 intent 也无 take——路由与装配没有依据"
    for p in unit.out:
        if p.slug.split("/", 1)[0] not in ("concepts", "entities", "topics"):
            return f"out slug 目录非法: {p.slug}——只能是 concepts/entities/topics"
        if p.slug not in existing and p.slug in unit.in_pages:
            return f"out {p.slug} 在 in 中却不存在于盘面"
        for t in p.take:
            if t.from_slug not in unit.in_pages:
                return f"take 来源 {t.from_slug} 不在本单元 in 中"
    return ""


def resolve_unit_conflicts(
    units: list[Unit], existing: set[str]
) -> tuple[list[Unit], list[tuple[Unit, str]]]:
    """单元间互斥：任何 slug（in 或 out）在一批里只属于一个单元；
    out 撞未被本批消费的现存页即冲突（不许悄悄覆盖别人的页）。"""
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
            dropped.append((unit, f"与第 {min(claimed[s] for s in clash)} 个单元争用页面: {clash}"))
            continue
        overwritten = [s for s in unit.out_slugs if s in existing and s not in set(unit.in_pages)]
        if overwritten:
            dropped.append((unit, f"输出页未被本单元消费却同名: {overwritten}——改写须进 in"))
            continue
        for s in touched:
            claimed[s] = len(clean)
        clean.append(unit)
    return clean, dropped


def assert_units_valid(units: list[Unit], existing: set[str]) -> None:
    """提交/执行前的硬校验：任何违例直接抛 UnitError（消解阶段已过滤，
    走到这里仍违例说明调用方绕过了消解——不静默放行）。"""
    clean, dropped = resolve_unit_conflicts(units, existing)
    if dropped:
        raise UnitError("; ".join(f"{u.in_pages}: {r}" for u, r in dropped))
    _ = clean
