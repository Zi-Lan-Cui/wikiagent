"""Refine 专用语义 Judge：比较单页 refine 前后的目标完成度。"""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.message import Message

REFINE_JUDGE_SYSTEM = """你是 Wiki 页面 refine 的语义评审员。你要比较同一个页面 refine 前后的内容，并判断这次更新是否真正完成页面使命，而不是只做表面改写。

只依据输入的 before 页面、after 页面、当前页面的 goal/gaps、search/analyze/plan 产物和候选页面判断，不使用外部知识补全。

输出必须是一个 JSON 对象，不要 Markdown：
{
  "gap_status": "reducible",
  "goal_completion": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "gap_reduction": {"score": 1, "applicable": true, "evidence": ["..."], "issues": ["..."]},
  "boundary_handling": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "source_fidelity": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "scope_discipline": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "relation_quality": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "regression_safety": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "unsupported_claims": ["没有输入材料支持的具体主张；没有则为空数组"],
  "confidence": 0.0,
  "verdict": "pass",
  "summary": "一句话总结"
}

每个 score 为 1-5：1=严重错误，3=部分完成或证据不足，5=充分完成。confidence 为 0 到 1。verdict 只能是 pass、review、fail：明显引入外部内容、破坏原有事实或没有完成可填充目标用 fail；有一定扩展、目标只部分完成或证据不足用 review；目标和缺口基本完成、范围克制且无明显幻觉才用 pass。

先判断 gap_status：
- reducible：输入材料明确提供了缺口所需事实，gap_reduction.applicable=true；应评价是否实际补充。
- unsupported：输入材料没有提供该信息，gap_reduction.applicable=false；不因缺口保留而扣分，必须评价 boundary_handling。
- conflicting：输入材料存在互相冲突的事实，gap_reduction.applicable=false；正确行为是保留冲突、标记待裁决，不得擅自选边。
- out_of_scope：缺口不属于当前页面 goal，gap_reduction.applicable=false；不扩展是正确行为。
- none：before 已经完成 goal 或没有可识别缺口；合法 no-op 不得因页面未变化而扣分。

材料不足时：明确写“未说明/无法从当前材料判断”是正确行为；臆造具体事实才是失败。gap_reduction.applicable=false 时，gap_reduction 的分数不能单独触发 review/fail，boundary_handling 才是主要评价维度。不要因为 after 字数增加或缺口数量减少就自动给高分。

特殊规则：如果 before 页面已经完成 goal 且没有 gaps，plan 合法地选择 no-op，这应视为正确完成，不因 after 与 before 相同而失败。"""

_DIMENSIONS = (
    "goal_completion",
    "gap_reduction",
    "boundary_handling",
    "source_fidelity",
    "scope_discipline",
    "relation_quality",
    "regression_safety",
)


def refine_judge_user(payload: dict[str, Any]) -> str:
    return (
        "## Refine 输入\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)[:60000]
        + "\n\n请基于 before/after 的具体差异给出 JSON 评分。"
    )


def check_refine_judge_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        return False, f"输出不是 JSON: {exc}"
    if not isinstance(data, dict):
        return False, "输出必须是 JSON 对象"
    required = set(_DIMENSIONS) | {
        "gap_status",
        "unsupported_claims",
        "confidence",
        "verdict",
        "summary",
    }
    missing = required - data.keys()
    if missing:
        return False, f"缺少字段: {sorted(missing)}"
    if data["gap_status"] not in {
        "reducible",
        "unsupported",
        "conflicting",
        "out_of_scope",
        "none",
    }:
        return False, "gap_status 必须是 reducible/unsupported/conflicting/out_of_scope/none"
    for key in _DIMENSIONS:
        value = data[key]
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("score"), int)
            or not 1 <= value["score"] <= 5
        ):
            return False, f"{key}.score 必须是 1-5 整数"
        if not isinstance(value.get("evidence"), list) or not isinstance(value.get("issues"), list):
            return False, f"{key} 必须包含 evidence/issues 数组"
        if key == "gap_reduction" and not isinstance(value.get("applicable"), bool):
            return False, "gap_reduction.applicable 必须是布尔值"
    if not isinstance(data["unsupported_claims"], list):
        return False, "unsupported_claims 必须是数组"
    if not isinstance(data["confidence"], (int, float)) or not 0 <= data["confidence"] <= 1:
        return False, "confidence 必须在 0-1"
    if data["verdict"] not in {"pass", "review", "fail"}:
        return False, "verdict 必须是 pass/review/fail"
    return True, ""


async def judge_refine_case(client, payload: dict[str, Any]) -> dict[str, Any]:
    response = await async_invoke_with_retry(
        client,
        [
            Message(role="system", content=REFINE_JUDGE_SYSTEM),
            Message(role="user", content=refine_judge_user(payload)),
        ],
        max_tokens=2400,
        temperature=0.0,
        extra_body={"thinking": {"type": "disabled"}},
        check=check_refine_judge_json,
        max_retries=2,
    )
    if not response.check_ok:
        raise RuntimeError(f"Refine Judge 输出校验失败: {response.check_reason}")
    return json.loads(_strip_fence(response.content))
