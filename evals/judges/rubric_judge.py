"""分档判定 Judge：一次调用给某阶段产物在全部维度上打 1–5 分并附依据。

评分员只按判据看产物与编译时信息面打分；其自身可靠性由双模型一致性与强制理由
核验发现（见 source-eval 的 judge 对照流程）。

硬约束：
- 模型只许引用 rubric 给出的维度 id，不许增删——回传集合与期望集合严格
  不等即判输出非法重试；漏评一维若被静默当成「该维无问题」，评分就失真。
"""

from __future__ import annotations

import json
import re
from typing import Any

from evals.core.reference_models import DimensionScore, StageRubric
from evals.judges.rendering import InputBlocks, render_blocks
from wiki_agent.compiler.integration.parse import strip_fence
from wiki_agent.conversation import Message
from wiki_agent.llm.params import JSON_MODE, NO_THINKING
from wiki_agent.llm.retry import async_invoke_with_retry

_DIMENSION_ID = re.compile(r"^[A-Z]\d")


def _normalize_dimension_id(raw: str) -> str:
    """评判模型常见偏差：把「P1 决策正确性 权重 0.55」整段抄进 dimension_id。
    前缀能对上唯一标识就取标识；对不上原样返回，交给集合校验判自创。"""
    match = _DIMENSION_ID.match(raw.strip())
    return match.group(0) if match else raw.strip()


def build_rubric_messages(
    stage: str,
    inputs: InputBlocks,
    artifact: InputBlocks,
    rubric: StageRubric,
) -> list[Message]:
    """组装分档评分 prompt：维度定义（五档描述）+ 编译时输入 + 被评产物。

    judge 只按判据看产物与编译时信息面打分，输入即评分所需全部依据。

    Args:
        stage: 阶段名。
        inputs: 该阶段编译时的信息面（rubric.visible_inputs_rule 的文件化），
            形态同 artifact。缺了它，「断言可溯源」类判据无从执行。
        artifact: 被评产物，形态同 inputs。
        rubric: 该阶段的 StageRubric。
    """
    dimension_blocks: list[str] = []
    for dimension in rubric.dimensions:
        lines = [
            f"### {dimension.id} {dimension.name}（权重 {dimension.weight:.2f}）",
            f"判据：{dimension.criteria}",
        ]
        lines.extend(f"{level} 分：{descriptor}" for level, descriptor in sorted(dimension.scoring.items(), reverse=True))
        dimension_blocks.append("\n".join(lines))
    system = "\n".join(
        [
            "你是知识库编译产物的分档评分员。你只依据给定的产物与判据打分，不使用任何外部知识。",
            "",
            "## 阶段",
            f"{stage}（本阶段判定信息面：{rubric.visible_inputs_rule}）",
            "",
            "## 评分维度与分档标准",
            "\n\n".join(dimension_blocks),
            "",
            "## 输出格式（JSON 对象）",
            '{"dimensions":[{"dimension_id":"...","rationale":"先判定后打分","score":1-5}]}',
            "",
            "注意：",
            "- 必须为每个给定维度回一个分数，不得遗漏、不得新增。",
            "- dimension_id 只回维度标识本身（大写字母加数字，如 P1、E2），不带名称、权重或括号说明。",
            "- 每个维度先写 rationale 再写 score：rationale 不超过 40 字，先陈述判定依据"
            "（如：对照输入里有无与本文重叠的既有页、产物具体返回/遗漏/改写了什么），再据这条依据定档。"
            "先给事实、后给分数——分数必须是理由推出来的，不能反过来。无 rationale 视为无效。",
            "- 判定对象为空集时（库里无相关既有页可召回、无候选页可判关系、本批只有一页），视为满足该条断言，按对应档满分判并在 rationale 说明，不因缺乏比较对象而跳过不评。",
        ]
    )
    user = "\n".join(
        [
            "## 编译时输入（判定唯一依据）",
            render_blocks(inputs),
            "",
            "## 被评产物（判定对象）",
            render_blocks(artifact),
        ]
    )
    return [Message(role="system", content=system), Message(role="user", content=user)]


def _check_rubric_json(content: str, expected_ids: set[str]) -> tuple[bool, str]:
    """形状校验：维度 id 集合与期望严格相等，且每维带非空 rationale。分数越界不在此拦——
    那是单条内容错误，交给 _dimension_scores 记 None，其余维度照收。缺理由视为无效：
    只有分数没有依据的记录无法核验 judge 是否读对了输入。"""
    try:
        data = json.loads(strip_fence(content))
    except Exception as exc:
        return False, f"评分输出不是 JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("dimensions"), list):
        return False, "缺少 dimensions 数组"
    got: set[str] = set()
    for item in data["dimensions"]:
        if not isinstance(item, dict) or not isinstance(item.get("dimension_id"), str):
            return False, "dimensions 元素缺少 dimension_id"
        if not str(item.get("rationale", "")).strip():
            return False, f"{item['dimension_id']} 缺 rationale"
        got.add(_normalize_dimension_id(item["dimension_id"]))
    if len(got) != len(data["dimensions"]):
        return False, "dimension_id 有重复"
    if got - expected_ids:
        return False, f"自创维度: {sorted(got - expected_ids)}"
    if expected_ids - got:
        return False, f"漏评维度: {sorted(expected_ids - got)}"
    return True, ""


def _dimension_scores(data: dict[str, Any]) -> list[DimensionScore]:
    """越界/非整数分数记 None（内容错误单条丢弃），其余照收。"""
    scores: list[DimensionScore] = []
    for item in data["dimensions"]:
        score = item.get("score")
        valid = isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 5
        scores.append(
            DimensionScore(
                dimension_id=_normalize_dimension_id(item["dimension_id"]),
                score=score if valid else None,
                rationale=str(item.get("rationale", "")),
            )
        )
    return scores


async def judge_rubric(
    client: Any,
    stage: str,
    inputs: InputBlocks,
    artifact: InputBlocks,
    rubric: StageRubric,
) -> dict[str, Any]:
    """一次调用产出该阶段全部维度的 1–5 分。

    Returns:
        {"dimensions": [DimensionScore...], "raw": dict, "usage": dict|None, "model": str}
    """
    expected_ids = {dimension.id for dimension in rubric.dimensions}
    messages = build_rubric_messages(stage, inputs, artifact, rubric)
    response = await async_invoke_with_retry(
        client,
        messages,
        check=lambda content: _check_rubric_json(content, expected_ids),
        max_tokens=4096,
        temperature=0.0,
        max_attempts=2,
        response_format=JSON_MODE,
        extra_body=NO_THINKING,
    )
    data = json.loads(strip_fence(response.content))
    # usage/model_id 走 getattr：client 是鸭子类型的 LLM 客户端，非 OpenAI 族
    # 可能不带 model_id、usage 可能无 model_dump，缺失留空而非抛错
    return {
        "dimensions": _dimension_scores(data),
        "raw": data,
        "usage": getattr(response.usage, "model_dump", lambda: None)(),
        "model": getattr(client, "model_id", ""),
    }
