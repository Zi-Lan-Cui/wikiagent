"""分档判定（离线）——prompt 组装、维度集合严格校验、越界分降级。

直接运行:  .venv/bin/python test/test_rubric_judge.py
"""

import json

import pytest

from evals.commands.reference_corpus import _GRADER_PATH
from evals.core.reference_models import load_grader
from evals.judges.rubric_judge import _check_rubric_json, _dimension_scores, build_rubric_messages

RUBRIC = load_grader(_GRADER_PATH).stage_rubrics["plan"]


def _messages_text() -> str:
    messages = build_rubric_messages(
        "plan",
        [("artifacts/a/extract.txt", "INPUT_BODY_X")],
        [("artifacts/a/plan.json", "PLAN_BODY_X")],
        RUBRIC,
    )
    text = messages[0].content + "\n" + messages[1].content
    assert "INPUT_BODY_X" in text and "PLAN_BODY_X" in text
    return text


def test_prompt_carries_all_dimensions_and_levels():
    text = _messages_text()
    for dimension in RUBRIC.dimensions:
        assert dimension.id in text and dimension.name in text
        assert f"{dimension.id} {dimension.name}" in text
    for level in range(1, 6):
        assert f"{level} 分：" in text
    assert "以该主题为主角" in text  # P1 criteria 修订必须随维度定义进 prompt


def test_case_set_never_enters_scoring_prompt():
    """题集内容不进评分 prompt——判卷员的考题属于探针链路，不是产物考卷的题面。"""
    assert "claim" not in _messages_text()


def _payload(dimensions: list[dict]) -> str:
    return json.dumps(
        {"dimensions": [{**d, "rationale": d.get("rationale", "据输入判")} for d in dimensions]}
    )


def test_check_json_strict_set_equality():
    expected = {"P1", "P2", "P3", "P4"}
    ok, _ = _check_rubric_json(
        _payload([{"dimension_id": f"P{i}", "score": 3} for i in range(1, 5)]), expected
    )
    assert ok
    missing, why = _check_rubric_json(_payload([{"dimension_id": "P1", "score": 3}]), expected)
    assert not missing and "漏评" in why
    invented, why = _check_rubric_json(
        _payload([{"dimension_id": f"P{i}", "score": 3} for i in range(1, 4)] + [{"dimension_id": "P9", "score": 3}]),
        expected,
    )
    assert not invented and "自创" in why
    duplicated, _ = _check_rubric_json(
        _payload([{"dimension_id": "P1", "score": 3}] * 4), expected
    )
    assert not duplicated


def test_out_of_range_scores_degrade_per_item():
    """越界是单条内容错误：该维记 None，其余维度照收，不废整批。"""
    data = json.loads(
        _payload(
            [
                {"dimension_id": "P1", "score": 7, "rationale": "越界"},
                {"dimension_id": "P2", "score": True, "rationale": "布尔冒充整数"},
                {"dimension_id": "P3", "score": 3, "rationale": "合法"},
            ]
        )
    )
    scores = {item.dimension_id: item.score for item in _dimension_scores(data)}
    assert scores == {"P1": None, "P2": None, "P3": 3}


def test_dimension_id_with_name_normalized():
    """评判模型把「P1 决策准确性」整段抄进 dimension_id 是实测常态——
    前缀能对上唯一标识就规范化后参与集合校验，否则 36 批全被自创拦成 judge_failed。"""
    expected = {"P1", "P2", "P3", "P4"}
    ok, _ = _check_rubric_json(
        _payload([{"dimension_id": f"P{i} 某某维度（权重 0.25）", "score": 3} for i in range(1, 5)]),
        expected,
    )
    assert ok
    normalized = {item.dimension_id for item in _dimension_scores(json.loads(_payload([
        {"dimension_id": "P1 决策准确性", "score": 4, "rationale": "r"},
    ])))}
    assert normalized == {"P1"}


def test_dimension_id_sneaky_duplicate_after_normalization_rejected():
    """带名与裸 id 混用：规范化后撞车仍须判重复，防一维双记分。"""
    expected = {"P1", "P2"}
    duplicated, _ = _check_rubric_json(
        _payload([{"dimension_id": "P1", "score": 3}, {"dimension_id": "P1 决策准确性", "score": 4}]),
        expected,
    )
    assert not duplicated


def test_dimension_id_unmatched_prefix_still_invented():
    expected = {"P1", "P2"}
    ok, why = _check_rubric_json(_payload([{"dimension_id": "维度一", "score": 3}]), expected)
    assert not ok and "自创" in why


def test_blank_rationale_rejected():
    """只给分数不给依据的记录无法核验 judge 是否读对输入——形状校验必须拒空 rationale。"""
    expected = {"P1"}
    bare = json.dumps({"dimensions": [{"dimension_id": "P1", "score": 4}]})
    ok, why = _check_rubric_json(bare, expected)
    assert not ok and "rationale" in why
    blank = json.dumps({"dimensions": [{"dimension_id": "P1", "score": 4, "rationale": "   "}]})
    assert not _check_rubric_json(blank, expected)[0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
