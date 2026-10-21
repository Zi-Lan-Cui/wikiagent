"""确定性校验（B 组验收）——每种 kind 正例反例，未知 kind 不得静默跳过。

直接运行:  .venv/bin/python test/test_deterministic.py
"""

import pytest

from evals.core.deterministic import run_checks
from evals.core.reference_models import DeterministicCheckRule


def _rule(**kwargs) -> DeterministicCheckRule:
    base = {"id": "T-1", "name": "测试", "kind": "range", "expr": "", "gate": True}
    base.update(kwargs)
    return DeterministicCheckRule.model_validate(base)


def test_b1_range_pass_and_b2_fail():
    rule = _rule(expr="60 <= chars <= 2000")
    assert run_checks("extract", {"chars": 855}, [rule])[0].passed is True
    assert run_checks("extract", {"chars": 10}, [rule])[0].passed is False


def test_range_on_text_artifact_uses_len():
    rule = _rule(expr="60 <= chars <= 2000")
    assert run_checks("extract", "摘" * 100, [rule])[0].passed is True
    with pytest.raises(ValueError, match="chars"):
        run_checks("extract", "x" * 100, [_rule(expr="60 <= artifact.chars <= 2000")])


def test_b3_regex_pass_and_b4_path_traversal_fail():
    rule = _rule(
        kind="regex",
        expr=r"^(entities|concepts|topics)/[a-z0-9-]+\.md$",
        pointer="page_targets[].wiki_path",
    )
    good = {"page_targets": [{"wiki_path": "entities/foo.md"}]}
    bad = {"page_targets": [{"wiki_path": "../etc/passwd"}]}
    assert run_checks("plan", good, [rule])[0].passed is True
    assert run_checks("plan", bad, [rule])[0].passed is False


def test_b5_unique_detects_duplicate():
    rule = _rule(kind="unique", pointer="page_targets[].wiki_path")
    duplicated = {
        "page_targets": [{"wiki_path": "entities/a.md"}, {"wiki_path": "entities/a.md"}]
    }
    assert run_checks("plan", duplicated, [rule])[0].passed is False
    assert run_checks("plan", {"page_targets": []}, [rule])[0].passed is True


def test_b6_unknown_kind_raises_not_silent():
    """未知 kind 在构造层就被模型拒绝；绕过校验硬塞进 run_checks 也必须抛，
    两条路都不许静默跳过——静默跳过等于悄悄放宽 gate。"""
    from pydantic import ValidationError

    from evals.core.reference_models import DeterministicCheckRule

    with pytest.raises(ValidationError):
        _rule(kind="foo")
    smuggled = DeterministicCheckRule.model_construct(
        id="T-1", name="测试", kind="foo", expr="", gate=True, pointer=""
    )
    with pytest.raises(ValueError, match="未知校验 kind"):
        run_checks("plan", {}, [smuggled])


def test_b7_formula_vacuous_and_b8_violated():
    rule = _rule(
        kind="formula",
        expr="index_pages == 0 => len(rel_paths) == 0",
        pointer="rel_paths",
    )
    ok = run_checks("search", {"rel_paths": []}, [rule], context={"index_pages": 0})
    assert ok[0].passed is True
    violated = run_checks("search", {"rel_paths": ["entities/a.md"]}, [rule], context={"index_pages": 0})
    assert violated[0].passed is False
    assert violated[0].gate is True
    # 前提不满足（库非空）时空真
    not_empty = run_checks("search", {"rel_paths": ["entities/a.md"]}, [rule], context={"index_pages": 3})
    assert not_empty[0].passed is True


def test_formula_unknown_quantity_raises():
    rule = _rule(kind="formula", expr="nonexistent == 0 => len(rel_paths) == 0")
    with pytest.raises(ValueError, match="未知量"):
        run_checks("search", {"rel_paths": []}, [rule])


def test_enum_and_schema():
    enum = _rule(kind="enum", expr="new|update", pointer="page_targets[].disposition")
    assert run_checks("plan", {"page_targets": [{"disposition": "new"}]}, [enum])[0].passed is True
    assert run_checks("plan", {"page_targets": [{"disposition": "no_op"}]}, [enum])[0].passed is False

    schema = _rule(kind="schema", expr="type,title,goal", pointer="pages[]")
    assert run_checks("execute", {"pages": [{"type": "entity", "title": "t", "goal": "g"}]}, [schema])[0].passed is True
    result = run_checks("execute", {"pages": [{"type": "entity"}]}, [schema])[0]
    assert result.passed is False and "缺字段" in result.detail


def test_missing_pointer_field_fails_loudly():
    """字段缺失是产物结构问题——必须报 fail 并说明，不能空真放行。"""
    rule = _rule(kind="unique", pointer="page_targets[].wiki_path")
    result = run_checks("plan", {"unrelated": 1}, [rule])[0]
    assert result.passed is False and "不存在" in result.detail


def test_text_artifact_rejects_non_range():
    with pytest.raises(ValueError, match="文本产物"):
        run_checks("plan", "纯文本", [_rule(kind="regex", expr="x", pointer="p")])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
