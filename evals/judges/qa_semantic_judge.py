"""QA 回答的独立 LLM Judge。"""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry

QA_JUDGE_SYSTEM = """你是本地 Wiki Agent 的问答质量评审员。
只依据题目、给出的证据页面和工具轨迹评估回答，不使用外部知识补全。
如果题目明确限定“仅凭笔记/Wiki”，模型即使把外部事实标注为“我的已有知识”或“非笔记来源”，也不能把该事实作为回答补充；这应降低 evidence_grounding、uncertainty_handling 和 answer_correctness。
必须区分“证据没有提供”与“回答错误”：题目材料不足时，回答明确说明未知/未说明是正确行为，不能因为没有补全而扣分。

输出严格为 JSON，不要 Markdown：
{
  "answer_correctness": {"score": 1, "evidence": [], "issues": []},
  "answer_completeness": {"score": 1, "evidence": [], "issues": []},
  "evidence_grounding": {"score": 1, "evidence": [], "issues": []},
  "uncertainty_handling": {"score": 1, "evidence": [], "issues": []},
  "cross_page_reasoning": {"score": 1, "evidence": [], "issues": []},
  "conversation_consistency": {"score": 1, "evidence": [], "issues": []},
  "unsupported_claims": [], "confidence": 0.0, "verdict": "pass", "summary": ""
}

每项 score 为 1-5：1 严重错误，3 部分正确或证据不足，5 正确完整且有证据。
对 fact 题重点看事实；compare/relation 题重点看区分和关系；uncertainty 题重点看是否拒绝臆造；followup 题重点看是否保留上下文。
verdict 只能为 pass/review/fail：任一维度 <=2 或有明显臆造为 fail；有维度为3或证据不足为 review；其余为 pass。"""

_DIMS = (
    "answer_correctness",
    "answer_completeness",
    "evidence_grounding",
    "uncertainty_handling",
    "cross_page_reasoning",
    "conversation_consistency",
)


def judge_user(case: dict[str, Any], record: dict[str, Any], evidence: dict[str, str]) -> str:
    return "\n".join(
        [
            "## 题目\n" + json.dumps(case, ensure_ascii=False, indent=2),
            "## 模型回答与工具轨迹\n" + json.dumps(record, ensure_ascii=False, indent=2)[:30000],
            "## Wiki 证据页面\n" + json.dumps(evidence, ensure_ascii=False, indent=2)[:50000],
            "请只输出约定的 JSON。",
        ]
    )


def check_qa_judge_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        return False, f"输出不是 JSON: {exc}"
    required = set(_DIMS) | {"unsupported_claims", "confidence", "verdict", "summary"}
    if not isinstance(data, dict):
        return False, "输出必须是 JSON 对象"
    missing = required - data.keys()
    if missing:
        return False, f"缺少字段: {sorted(missing)}"
    for key in _DIMS:
        value = data[key]
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("score"), int)
            or not 1 <= value["score"] <= 5
        ):
            return False, f"{key}.score 必须是 1-5 整数"
        if not isinstance(value.get("evidence"), list) or not isinstance(value.get("issues"), list):
            return False, f"{key} 必须包含 evidence/issues 数组"
    if not isinstance(data["unsupported_claims"], list):
        return False, "unsupported_claims 必须是数组"
    if not isinstance(data["confidence"], (int, float)) or not 0 <= data["confidence"] <= 1:
        return False, "confidence 必须在 0-1"
    if data["verdict"] not in {"pass", "review", "fail"}:
        return False, "verdict 必须是 pass/review/fail"
    return True, ""


async def judge_qa_case(
    client, case: dict[str, Any], record: dict[str, Any], evidence: dict[str, str]
) -> dict[str, Any]:
    response = await async_invoke_with_retry(
        client,
        [
            Message(role="system", content=QA_JUDGE_SYSTEM),
            Message(role="user", content=judge_user(case, record, evidence)),
        ],
        max_tokens=2400,
        temperature=0.0,
        extra_body={"thinking": {"type": "disabled"}},
        check=check_qa_judge_json,
        max_retries=2,
    )
    if not response.check_ok:
        raise RuntimeError(f"QA Judge 输出校验失败: {response.check_reason}")
    return json.loads(_strip_fence(response.content))
