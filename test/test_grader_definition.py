"""评分器定义（A 组验收）——模型约束与语料侧校验。

直接运行:  .venv/bin/python test/test_grader_definition.py
"""

import copy
import json

import pytest
from pydantic import ValidationError

from evals.commands.reference_corpus import _GRADER_PATH, validate_grader
from evals.core.reference_models import GraderDefinition, load_grader


@pytest.fixture(scope="module")
def grader_dict() -> dict:
    return json.loads(_GRADER_PATH.read_text(encoding="utf-8"))


def test_a1_real_definition_constructs(grader_dict: dict):
    grader = GraderDefinition.model_validate(grader_dict)
    assert set(grader.stage_rubrics) == {"extract", "analyze", "plan", "search", "execute"}
    assert len(load_grader(_GRADER_PATH).stage_rubrics) == 5


def test_a2_missing_scoring_level_rejected(grader_dict: dict):
    broken = copy.deepcopy(grader_dict)
    broken["stage_rubrics"]["extract"]["dimensions"][0]["scoring"].pop("3")
    with pytest.raises(ValidationError, match="缺少分档描述"):
        GraderDefinition.model_validate(broken)


def test_a3_weights_must_sum_to_one(grader_dict: dict):
    broken = copy.deepcopy(grader_dict)
    broken["stage_rubrics"]["plan"]["dimensions"][0]["weight"] = 0.5
    with pytest.raises(ValidationError, match="权重和必须为 1.0"):
        GraderDefinition.model_validate(broken)


def test_a4_must_cover_all_five_stages(grader_dict: dict):
    broken = copy.deepcopy(grader_dict)
    broken["stage_rubrics"].pop("search")
    with pytest.raises(ValidationError, match="必须覆盖五阶段"):
        GraderDefinition.model_validate(broken)


def test_unknown_stage_in_deterministic_checks_rejected(grader_dict: dict):
    """S-EMPTY 写成 s-EMPTY 这类笔误必须当场炸，不能静默不生效。"""
    broken = copy.deepcopy(grader_dict)
    broken["deterministic_checks"]["s-EMPTY"] = broken["deterministic_checks"].pop("search")
    with pytest.raises(ValidationError, match="未知阶段"):
        GraderDefinition.model_validate(broken)


def test_c1_anchor_carried_in_p1_criteria(grader_dict: dict):
    """判据的唯一载体是维度定义：主角地位条款与 fencing token 示例必须在 P1 criteria 内；
    定义文件不设 judge_protocol 之类的旁路协议块。"""
    p1 = grader_dict["stage_rubrics"]["plan"]["dimensions"][0]
    assert "以该主题为主角" in p1["criteria"]
    assert "fencing token" in p1["criteria"]
    assert "judge_protocol" not in grader_dict


def test_weight_of_rejects_unknown_dimension(grader_dict: dict):
    grader = GraderDefinition.model_validate(grader_dict)
    rubric = grader.stage_rubrics["plan"]
    assert rubric.weight_of("P1") == 0.55
    with pytest.raises(ValueError, match="未知维度"):
        rubric.weight_of("P9")


def test_validate_grader_clean_on_repo_file():
    assert validate_grader() == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
