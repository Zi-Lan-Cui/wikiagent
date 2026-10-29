"""确定性校验执行器——按 kind 分派到具名函数，禁止 eval 任意字符串。"""

from __future__ import annotations

import re
from typing import Any

from evals.core.reference_models import CheckResult, DeterministicCheckRule

_RANGE_PATTERN = re.compile(r"^\s*(\d+)\s*<=\s*([\w.]+)\s*<=\s*(\d+)\s*$")
_RELATION_PATTERN = re.compile(r"^\s*(\w+|len\(\w+\))\s*(==|!=|in)\s*(.+?)\s*$")


def _leaf_values(artifact: dict, pointer: str) -> tuple[bool, list]:
    """按 pointer 取叶子值：`page_targets[].wiki_path` 展开列表逐元素再取字段。"""
    if not pointer:
        return True, [artifact]
    segments = pointer.split(".")
    first_name = segments[0][:-2] if segments[0].endswith("[]") else segments[0]
    if not isinstance(artifact, dict) or first_name not in artifact:
        return False, []
    values: list[Any] = [artifact]
    for segment in segments:
        is_many = segment.endswith("[]")
        name = segment[:-2] if is_many else segment
        next_values: list[Any] = []
        for value in values:
            if not isinstance(value, dict) or name not in value:
                continue
            item = value[name]
            if is_many:
                if not isinstance(item, list):
                    raise ValueError(f"pointer {pointer!r}: {name} 不是列表")
                next_values.extend(item)
            else:
                next_values.append(item)
        values = next_values
    return True, values


def _check_range(
    rule: DeterministicCheckRule, artifact: Any, context: dict
) -> tuple[bool, str]:
    match = _RANGE_PATTERN.match(rule.expr)
    if not match:
        raise ValueError(f"range 表达式应形如 MIN <= <路径> <= MAX，实际 {rule.expr!r}")
    low, path, high = int(match.group(1)), match.group(2), int(match.group(3))
    if isinstance(artifact, str):
        if path != "chars":
            raise ValueError(f"文本产物只支持 chars 路径，规则 {rule.id} 写了 {path!r}")
        value = len(artifact)
    else:
        found, values = _leaf_values(artifact, path)
        if not found or not values:
            return False, f"路径 {path} 不可解析"
        value = values[0]
    if not isinstance(value, int):
        return False, f"{path} 不是整数: {value!r}"
    if low <= value <= high:
        return True, ""
    return False, f"{path}={value} 超出 [{low}, {high}]"


def _check_regex(
    rule: DeterministicCheckRule, artifact: Any, context: dict
) -> tuple[bool, str]:
    found, values = _leaf_values(artifact, rule.pointer)
    if not found:
        return False, f"pointer {rule.pointer!r} 指向的字段不存在"
    pattern = re.compile(rule.expr)
    for value in values:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return False, f"{rule.pointer} 存在不匹配值: {value!r}"
    return True, ""


def _check_unique(
    rule: DeterministicCheckRule, artifact: Any, context: dict
) -> tuple[bool, str]:
    found, values = _leaf_values(artifact, rule.pointer)
    if not found:
        return False, f"pointer {rule.pointer!r} 指向的字段不存在"
    seen: set = set()
    for value in values:
        if value in seen:
            return False, f"重复值: {value!r}"
        seen.add(value)
    return True, ""


def _check_enum(
    rule: DeterministicCheckRule, artifact: Any, context: dict
) -> tuple[bool, str]:
    allowed = {item.strip() for item in rule.expr.split("|") if item.strip()}
    if not allowed:
        raise ValueError(f"enum 表达式为空: {rule.expr!r}（格式 a|b|c）")
    found, values = _leaf_values(artifact, rule.pointer)
    if not found:
        return False, f"pointer {rule.pointer!r} 指向的字段不存在"
    for value in values:
        if str(value) not in allowed:
            return False, f"非法枚举值 {value!r}，允许 {sorted(allowed)}"
    return True, ""


def _check_schema(
    rule: DeterministicCheckRule, artifact: Any, context: dict
) -> tuple[bool, str]:
    """逗号分隔的必需字段；pointer 指向列表时对每个元素分别要求。"""
    required = [field.strip() for field in rule.expr.split(",") if field.strip()]
    if not required:
        raise ValueError(f"schema 表达式为空: {rule.expr!r}")
    found, targets = _leaf_values(artifact, rule.pointer)
    if not found:
        return False, f"pointer {rule.pointer!r} 指向的字段不存在"
    for target in targets:
        if not isinstance(target, dict):
            return False, f"schema 检查目标不是对象: {type(target).__name__}"
        missing = [field for field in required if field not in target]
        if missing:
            return False, f"缺字段 {missing}"
    return True, ""


def _fetch_quantity(name: str, artifact: Any, context: dict) -> Any:
    if name in context:
        return context[name]
    if isinstance(artifact, dict) and name in artifact:
        return artifact[name]
    raise ValueError(f"formula 引用了未知量 {name!r}（context 与产物都没有）")


def _eval_relation(text: str, artifact: Any, context: dict) -> bool:
    match = _RELATION_PATTERN.match(text)
    if not match:
        raise ValueError(f"formula 子句格式非法: {text!r}")
    left, operator, right = match.groups()
    if left.startswith("len("):
        value = len(_fetch_quantity(left[4:-1], artifact, context))
    else:
        value = _fetch_quantity(left, artifact, context)
    if operator == "in":
        if not (right.startswith("{") and right.endswith("}")):
            raise ValueError(f"in 右值应为 {{a,b,c}}，实际 {right!r}")
        return str(value) in {item.strip() for item in right[1:-1].split(",")}
    try:
        target = int(right)
    except ValueError as exc:
        raise ValueError(f"== / != 右值应为整数，实际 {right!r}") from exc
    if operator == "==":
        return value == target
    return value != target


def _check_formula(
    rule: DeterministicCheckRule, artifact: Any, context: dict
) -> tuple[bool, str]:
    if "=>" not in rule.expr:
        raise ValueError(f"formula 应为 条件 => 断言，实际 {rule.expr!r}")
    condition, assertion = (part.strip() for part in rule.expr.split("=>", 1))
    if not _eval_relation(condition, artifact, context):
        # 前提不满足则规则空真：index_pages==0 只约束空库场景
        return True, "前提不满足，空真通过"
    if _eval_relation(assertion, artifact, context):
        return True, ""
    return False, f"断言违反: {assertion}"


_CHECKS = {
    "range": _check_range,
    "regex": _check_regex,
    "unique": _check_unique,
    "enum": _check_enum,
    "schema": _check_schema,
    "formula": _check_formula,
}


def run_checks(
    stage: str,
    artifact: Any,
    rules: list[DeterministicCheckRule],
    context: dict | None = None,
) -> list[CheckResult]:
    """按 kind 分派执行校验规则，返回逐条结果（含 gate 位，随记录落盘）。

    Args:
        stage: 阶段名，仅用于报错定位。
        artifact: dict（JSON 产物）或 str（文本产物）。
        rules: 该阶段的 DeterministicCheckRule 列表。
        context: formula 可引用的派生量（如 index_pages）。
    """
    context = context or {}
    results: list[CheckResult] = []
    for rule in rules:
        check = _CHECKS.get(rule.kind)
        if check is None:
            raise ValueError(f"阶段 {stage} 未知校验 kind: {rule.kind!r}")
        if isinstance(artifact, str) and (rule.kind != "range" or rule.pointer):
            raise ValueError(f"文本产物只支持 kind=range 且路径 chars，规则 {rule.id} 违反")
        passed, detail = check(rule, artifact, context)
        results.append(
            CheckResult(rule_id=rule.id, passed=passed, gate=rule.gate, detail=detail)
        )
    return results
