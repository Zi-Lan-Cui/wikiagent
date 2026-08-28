"""基于 LLM 的语义评测器。

Judge 只负责给出带证据的语义意见；硬性契约仍由 stage_harness 判定。
输出固定 JSON，便于保存、复核和跨模型重复运行。
"""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.message import Message

JUDGE_SYSTEM = """你是知识库编译质量评审员。你要评估一个源文档经过 extract、search、analyze、plan 后生成 Wiki 页面是否忠实、相关、组织合理。

只依据输入材料判断，不使用外部知识补全。不要因为页面数量多就自动判错：只要每个页面有独立主题且有源文档证据，可以拆分。也不要因为页面数量少就自动判对：如果多个独立主题被混在一起，应指出。

输出必须是一个 JSON 对象，不要 Markdown，不要解释 JSON 以外的内容：
{
  "source_fidelity": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "search_relevance": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "analysis_quality": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "plan_quality": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "page_quality": {"score": 1, "evidence": ["..."], "issues": ["..."]},
  "unsupported_claims": ["没有源文档支持的具体主张；没有则为空数组"],
  "confidence": 0.0,
  "verdict": "pass",
  "summary": "一句话总结"
}

评分规则：每个 score 为 1-5 的整数：1=严重错误，3=有明显问题但可用，5=充分正确。confidence 为 0 到 1。verdict 只能是 pass、review、fail：存在任一维度 <=2 或明显幻觉用 fail；存在维度为3或证据不足用 review；全部 >=4 且没有明显幻觉才用 pass。
重点检查：摘要是否保留关键事实；search 候选是否与源文档主题相关；analyze 的实体/概念/关系是否有源文档和候选页支持；plan 是否做出恰当的 new/update/no-op 决策；生成页面是否相关、重复、过度拆分或引入外部结论。"""

# 评测规则补充：信息不足不是失败。空正文/只有标题的 source 如果被摘要
# 标注为信息不足、search/analyze 为空、plan no-op，说明系统正确阻止了
# 标题驱动的幻觉页面，应按“适当跳过”评分，而不是因没有页面机械判 fail。
JUDGE_SYSTEM += "\n\n特殊情况：如果源文档确实没有实质内容，且 extract 明确标注信息不足、search/analyze 为空、plan 选择 no-op，则这是正确结果；相关维度按 4-5 分评估，page_quality 记为 5（正确避免生成无依据页面），verdict 不得仅因没有页面而判 fail。"


def judge_user(
    case: dict[str, Any], source: str, artifacts: dict[str, Any], pages: list[dict[str, str]]
) -> str:
    cluster = case.get("cluster", {})
    return "\n".join(
        [
            f"## 样本 {case.get('id', '')}",
            f"评估目标: {cluster.get('goal', '')}",
            f"参考页面（仅作组织提示，不是必须逐字匹配）: {cluster.get('expected_pages', [])}",
            f"参考关系（仅作组织提示）: {cluster.get('expected_relations', [])}",
            f"摘要必须保留: {case.get('must_include', [])}",
            f"禁止臆造: {case.get('must_not_invent', [])}",
            "\n## 源文档\n" + source[:24000],
            "\n## 阶段产物\n" + json.dumps(artifacts, ensure_ascii=False, indent=2)[:30000],
            "\n## 生成页面\n" + json.dumps(pages, ensure_ascii=False, indent=2)[:50000],
        ]
    )


def check_judge_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        return False, f"Judge 输出不是 JSON: {exc}"
    if not isinstance(data, dict):
        return False, "Judge 输出必须是 JSON 对象"
    required = {
        "source_fidelity",
        "search_relevance",
        "analysis_quality",
        "plan_quality",
        "page_quality",
        "unsupported_claims",
        "confidence",
        "verdict",
        "summary",
    }
    missing = required - data.keys()
    if missing:
        return False, f"缺少字段: {sorted(missing)}"
    for key in required - {"unsupported_claims", "confidence", "verdict", "summary"}:
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
        max_tokens=2600,
        temperature=0.0,
        extra_body={"thinking": {"type": "disabled"}},
        check=check_judge_json,
        max_retries=2,
    )
    if not response.check_ok:
        raise RuntimeError(f"Judge 输出校验失败: {response.check_reason}")
    return json.loads(_strip_fence(response.content))
