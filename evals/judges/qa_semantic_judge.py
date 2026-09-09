"""QA 回答的二元 LLM Judge（对齐原则4：二元+弃权，不用 1-5 Likert）。

每个维度 verdict ∈ {pass, fail, unknown} + evidence/issues；另含逐论断
grounding（论断是否有被引页支撑）与 citation_support（引用是否真支撑）。
unknown=材料不足以可靠判断，弃权不计入分母；uncertainty_handling 在证据不足时
明确说"未说明"是 pass（与"是否臆造"配套）。
"""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry

_VERDICTS = {"pass", "fail", "unknown"}

QA_JUDGE_SYSTEM = """你是本地 Wiki Agent 问答质量的二元评审员。只依据题目、给出的证据页面和工具轨迹判断，不使用外部知识补全。

对每个维度给 {verdict: pass|fail|unknown, evidence: [...], issues: [...]}；unknown 表示证据不足以可靠判断，不得强行猜：
- answer_correctness：结论是否属实（相对证据页）。
- answer_completeness：是否覆盖题目要求的关键点。
- grounding：逐条枚举回答中的关键论断，判 supported / unsupported / unknown（源材料能否直接支撑）。
- citation_support：答案给出的每条 [[引用]] 是否确为该论断的来源（页存在且含该论断）。
- uncertainty_handling：证据不足时，明确说"未说明/无法判断"=pass；把无依据事实当结论=fail。题目限定"仅凭笔记"时，引入外部知识=fail。
- conversation_consistency：多轮是否正确承接上下文。

overall verdict：任一关键 fail 或存在无依据论断=critical unsupported→fail；有 unknown 或非关键缺口→review；全部 supported 且覆盖且弃权得当→pass。
只输出 JSON：
{
  "answer_correctness": {"verdict": "pass", "evidence": [], "issues": []},
  "answer_completeness": {"verdict": "pass", "evidence": [], "issues": []},
  "grounding": {"claims": [{"claim": "", "verdict": "supported", "evidence": ""}]},
  "citation_support": {"citations": [{"citation": "concepts/x.md", "verdict": "supported", "note": ""}]},
  "uncertainty_handling": {"verdict": "pass", "evidence": [], "issues": []},
  "conversation_consistency": {"verdict": "pass", "evidence": [], "issues": []},
  "unsupported_claims": [], "unknown_reasons": [], "verdict": "pass", "summary": ""
}"""

_DIMS = (
    "answer_correctness",
    "answer_completeness",
    "uncertainty_handling",
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
    if not isinstance(data, dict):
        return False, "输出必须是 JSON 对象"
    required = set(_DIMS) | {
        "grounding",
        "citation_support",
        "unsupported_claims",
        "unknown_reasons",
        "verdict",
        "summary",
    }
    missing = required - data.keys()
    if missing:
        return False, f"缺少字段: {sorted(missing)}"
    for key in _DIMS:
        value = data[key]
        if not isinstance(value, dict) or value.get("verdict") not in _VERDICTS:
            return False, f"{key}.verdict 必须是 pass/fail/unknown"
    claims = data["grounding"].get("claims")
    if not isinstance(claims, list):
        return False, "grounding.claims 必须是数组"
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("verdict") not in {
            "supported",
            "unsupported",
            "unknown",
        }:
            return False, "grounding claim verdict 非法"
    cites = data["citation_support"].get("citations")
    if not isinstance(cites, list):
        return False, "citation_support.citations 必须是数组"
    for cite in cites:
        if not isinstance(cite, dict) or cite.get("verdict") not in {
            "supported",
            "unsupported",
            "unknown",
        }:
            return False, "citation_support verdict 非法"
    if not isinstance(data["unsupported_claims"], list) or not isinstance(
        data["unknown_reasons"], list
    ):
        return False, "unsupported_claims/unknown_reasons 必须是数组"
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
        max_tokens=2600,
        temperature=0.0,
        extra_body={"thinking": {"type": "disabled"}},
        check=check_qa_judge_json,
        max_retries=2,
    )
    if not response.check_ok:
        raise RuntimeError(f"QA Judge 输出校验失败: {response.check_reason}")
    return json.loads(_strip_fence(response.content))
