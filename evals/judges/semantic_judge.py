"""Binary outcome judge for groundedness and key-fact coverage."""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry

JUDGE_SYSTEM = """你是知识库编译结果评审员，只评价最终 Wiki 页面，不评价 Agent 采用的搜索、分析或工具调用路径。

请完成两个相互独立的二元检查：
1. grounding：枚举生成页面中的重要事实论断，逐条判断 supported / unsupported / unknown。只有输入源材料能够直接支持时才是 supported。
2. coverage：对输入给出的 required_facts 逐条判断 covered / missing / unknown，并给出生成页面中的证据。

unknown 表示材料不足或无法可靠判断，不要强行猜测。关键事实 unsupported 时整题必须 fail；存在 unknown 时至少 review。
只输出 JSON：
{
  "grounding": {"claims": [{"claim": "事实论断", "verdict": "supported", "evidence": "源材料证据", "critical": true}]},
  "coverage": {"facts": [{"fact_id": "fact-01", "verdict": "covered", "evidence": "生成页面证据"}]},
  "unknown_reasons": [],
  "verdict": "pass",
  "summary": "一句话结论"
}

verdict 只能是 pass/review/fail。所有重要论断有依据且 required_facts 全覆盖才可 pass；关键臆造或关键事实遗漏为 fail；存在非关键遗漏、unknown 或证据歧义为 review。
如果源材料没有实质内容且系统明确 no-op、不生成页面，这是正确结果。"""


def judge_user(
    case: dict[str, Any], source: str, artifacts: dict[str, Any], pages: list[dict[str, str]]
) -> str:
    required = case.get("required_facts", case.get("must_include", []))
    return "\n".join(
        [
            f"## 样本 {case.get('id', '')}",
            "required_facts: " + json.dumps(required, ensure_ascii=False),
            "forbidden_claims: "
            + json.dumps(
                case.get("forbidden_claims", case.get("must_not_invent", [])),
                ensure_ascii=False,
            ),
            "expected_behavior: "
            + json.dumps(case.get("expected_behavior", {}), ensure_ascii=False),
            "\n## 源材料\n" + source[:30000],
            "\n## 生成页面\n" + json.dumps(pages, ensure_ascii=False, indent=2)[:60000],
            "\n## 过程产物（只用于定位证据，不对路径评分）\n"
            + json.dumps(artifacts, ensure_ascii=False, indent=2)[:20000],
        ]
    )


def check_judge_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        return False, f"Judge 输出不是 JSON: {exc}"
    if not isinstance(data, dict):
        return False, "Judge 输出必须是 JSON 对象"
    required = {"grounding", "coverage", "unknown_reasons", "verdict", "summary"}
    missing = required - data.keys()
    if missing:
        return False, f"缺少字段: {sorted(missing)}"
    claims = data.get("grounding", {}).get("claims")
    facts = data.get("coverage", {}).get("facts")
    if not isinstance(claims, list):
        return False, "grounding.claims 必须是数组"
    if not isinstance(facts, list):
        return False, "coverage.facts 必须是数组"
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("verdict") not in {
            "supported",
            "unsupported",
            "unknown",
        }:
            return False, "grounding claim verdict 非法"
        if not isinstance(claim.get("claim"), str) or not isinstance(claim.get("evidence"), str):
            return False, "grounding claim 缺少 claim/evidence"
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("verdict") not in {
            "covered",
            "missing",
            "unknown",
        }:
            return False, "coverage fact verdict 非法"
        if not isinstance(fact.get("fact_id"), str) or not isinstance(fact.get("evidence"), str):
            return False, "coverage fact 缺少 fact_id/evidence"
    if not isinstance(data["unknown_reasons"], list):
        return False, "unknown_reasons 必须是数组"
    if data["verdict"] not in {"pass", "review", "fail"}:
        return False, "verdict 必须是 pass/review/fail"
    return True, ""


async def judge_case(
    client,
    case: dict[str, Any],
    source: str,
    artifacts: dict[str, Any],
    pages: list[dict[str, str]],
) -> dict[str, Any]:
    response = await async_invoke_with_retry(
        client,
        [
            Message(role="system", content=JUDGE_SYSTEM),
            Message(role="user", content=judge_user(case, source, artifacts, pages)),
        ],
        max_tokens=3000,
        temperature=0.0,
        extra_body={"thinking": {"type": "disabled"}},
        check=check_judge_json,
        max_retries=2,
    )
    if not response.check_ok:
        raise RuntimeError(f"Judge 输出校验失败: {response.check_reason}")
    return json.loads(_strip_fence(response.content))
