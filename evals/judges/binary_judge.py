"""二元判定 Judge：逐条判定 claim 是否受编译输入支持并真实存在于产出。

- judge 只拿题目 view 声明的输入与产出文件，逐条给出 true/false/unknown。
- unknown 是合法弃权，不计入一致率。
- 判定标准（四维度）见 evals/corpora/reference-v1/verdicts/README.md。
"""

from __future__ import annotations

from typing import Any

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry

JUDGE_SYSTEM = """你是知识库编译过程的二元判定员。你的判定必须只依据给定的输入与产出——输入是编译该产出时唯一可用的素材，不允许用输入之外的任何信息（外部知识、训练记忆）补全或纠错。

对每道题给出一条 claim，判定 claim 成立（true）还是失败（false）：
- 判定成立：claim 的断言既受输入支持，又真实存在于产出中（同义改写算存在）。
- 判定失败：输入不支持（臆造/夸大）或产出中没有（遗漏/丢失）。
- 无法可靠判定时输出 "unknown"，不要强行猜测。

注意：
- 数字、参数、限定词要与输入一致；输入明言的缺口/未实现状态，产出写成已实现就是失败。
- 输入只给了方向/术语时，产出给出正式定义、公式或具体机制，属于超出输入支持的添加。
- 拆分/建页类 claim 的判定标准（C1/C3，唯一标准）：
  C1 独立定义段：输入（摘要/分析）中该主题是否有独立论述段——≥2 个独立事实（定义/机制/边界/案例各算一个事实），一句话提及不算独立论述段。
  C3 职责重叠：输入中的 structure-tree 里，是否有既有页面的 goal 已声明覆盖该主题（goal 文本同义即算覆盖）。
  判定规则：claim 断言『决策成立（C1 ∧ ¬C3）』时，C1 不成立或 C3 成立 → false；claim 断言『决策不成立』时同理。只依据输入中的内容量与结构树 goal 判定，不评价未知的未来价值。
- 不评价编译过程本身，只判定给定输入与产出之间的忠实关系。

输出格式（纯 JSON，不要其他内容）：
{"judgements": [{"id": "...", "verdict": true, "evidence": "产出某处 + 输入某处", "reason": "一句话依据"}]}
"""


def build_judge_messages(
    source: str, page: str | list[str], claims: list[dict[str, Any]]
) -> list[Message]:
    """组装二元判定 prompt——编译输入素材 + 产出 + 待判 claim 清单。"""
    page_text = "\n\n".join(page) if isinstance(page, list) else page
    return [
        Message(role="system", content=JUDGE_SYSTEM),
        Message(
            role="user",
            content="\n".join(
                [
                    "## 编译输入（判定唯一依据）",
                    source[:12000],
                    "",
                    "## 编译产出（判定对象）",
                    page_text[:12000],
                    "",
                    "## 待判定的 claim 清单",
                    "\n".join(f"- {c['id']}: {c['claim']}" for c in claims),
                ]
            ),
        ),
    ]


def check_judge_json(content: str, claim_ids: set[str]) -> tuple[bool, str]:
    """校验 judge 输出——judgements 齐全、id 与 verdict 合法。"""
    try:
        data = __import__("json").loads(_strip_fence(content))
    except Exception as exc:  # JSONDecodeError 等
        return False, f"Judge 输出不是 JSON: {exc}"
    if not isinstance(data, dict) or "judgements" not in data:
        return False, "缺少 judgements 字段"
    judgements = data["judgements"]
    if not isinstance(judgements, list) or len(judgements) != len(claim_ids):
        return False, f"judgements 数量不符: 期望 {len(claim_ids)}"
    got: set[str] = set()
    for item in judgements:
        if not isinstance(item, dict):
            return False, "judgements 元素必须是对象"
        if item.get("id") not in claim_ids:
            return False, f"未知 id: {item.get('id')}"
        if item.get("verdict") not in (True, False, None, "unknown"):
            return False, f"verdict 必须是 true/false/unknown: {item.get('id')}"
        got.add(item["id"])
    if got != claim_ids:
        return False, f"id 不齐全: 缺 {sorted(claim_ids - got)}"
    return True, ""


async def judge_claims(
    client, source: str, page: str | list[str], claims: list[dict[str, Any]]
) -> dict[str, Any]:
    """逐批判定 claim 清单，返回结构化判定 + trace 元信息。"""
    claim_ids = {c["id"] for c in claims}
    messages = build_judge_messages(source, page, claims)
    response = await async_invoke_with_retry(
        client,
        messages,
        check=lambda content: check_judge_json(content, claim_ids),
        max_tokens=4096,
        temperature=0.0,
        max_retries=2,
    )
    import json

    data = json.loads(_strip_fence(response.content))
    verdict_map: dict[str, Any] = {}
    for item in data["judgements"]:
        value = item.get("verdict")
        # unknown 归一为 None（与金标的 bool 区分开，不计入一致率分母）
        verdict_map[item["id"]] = None if value in ("unknown", None) else bool(value)
    return {
        "verdicts": verdict_map,
        "raw": data,
        "usage": getattr(response.usage, "model_dump", lambda: None)(),
        "model": getattr(client, "model_id", ""),
    }
