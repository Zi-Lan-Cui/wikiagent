"""结构重组的候选提议——LLM 见全库 index，提原子操作（merge/delete/create/trim）。

LLM 只在本模块与 review 出现（蓝图约束）。
"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.integration.parse import _strip_fence
from wiki_agent.compiler.models import _NO_THINKING
from wiki_agent.compiler.restructure.common import _index_overview, _safe_parse_json
from wiki_agent.compiler.restructure.models import (
    _PROPOSE_MAX_COUNT,
    _PROPOSE_MAX_TOKENS,
    Proposal,
)
from wiki_agent.conversation import Message
from wiki_agent.errors import translate_generic_error
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import emit_event, get_logger

logger = get_logger("RESTRUCTURE")


def _check_propose_list(content: str) -> tuple[bool, str]:
    """校验粗提输出——原子提议数组。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 错误消息)。
    """
    import json as _json

    # fence 剥离统一走 integration.parse._strip_fence（与 check/parse 层同一
    # 规约，含 I5 尾部括号 repair——重组的 LLM 输出同样可能缺尾括号）
    cleaned = _strip_fence(content)
    try:
        data = _json.loads(cleaned)
    except _json.JSONDecodeError as e:
        return False, f"JSON 格式错误: {e}"
    if not isinstance(data, list):
        return False, "输出必须是数组（无提议输出 []）"
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return False, f"[{i}] 必须是对象"
        if item.get("op") not in ("merge", "delete", "create", "trim"):
            return False, f"[{i}].op 必须是 merge/delete/create/trim，当前: {item.get('op')!r}"
        if not isinstance(item.get("pages", []), list) or not item["pages"]:
            return False, f"[{i}].pages 必须是非空数组"
        if not str(item.get("reason", "")).strip():
            return False, f"[{i}].reason 不能为空"
        if item.get("op") in ("create", "trim"):
            if len(item["pages"]) != 1:
                return False, f"[{i}].{item['op']} 只能涉及一个已有页面"
            if not isinstance(item.get("sections", []), list) or not item["sections"]:
                return False, f"[{i}].sections 必须是非空数组"
        if item.get("op") == "create" and not str(item.get("target", "")).strip():
            return False, f"[{i}].target 必须是新页面路径"
        if item.get("id") and not isinstance(item["id"], str):
            return False, f"[{i}].id 必须是字符串"
        if item.get("depends_on") and not isinstance(item["depends_on"], list):
            return False, f"[{i}].depends_on 必须是数组"
    return True, ""


async def propose_from_index(llm, wiki_dir: str | Path) -> list[Proposal]:
    """粗提——一次 LLM 调用，index 全量可见，输出 flat 原子提议数组。

    Args:
        llm: LLM 客户端。
        wiki_dir: wiki 根目录。

    Returns:
        提议列表；LLM 失败/坏内容时降级为空列表（不崩）。
    """
    overview = _index_overview(wiki_dir)
    prompt = "\n\n".join(
        [
            "你是知识库的结构审查员。基于全库页面索引，提出结构重组的原子提议。",
            "",
            "## 高度重叠的强信号（出现任一就该提 merge）",
            "- 两个页面摘要围绕同一组核心术语（如都围绕 *args/**kwargs、"
            "深浅拷贝、装饰器元数据）——即使标题不同",
            "- 一页的主题在另一页摘要中作为其中一个主题出现（一页是另一页的章节）",
            "- 摘要措辞不同但回答的是同一个问题",
            "",
            "## 可提的原子操作（每条提议独立、不可再分）",
            "- merge: 两个页面内容高度重叠 → 合并为一个（一个吸收另一个）。"
            "pages 是两个 slug，方向在复判阶段定。",
            "- delete: 页面无独立存在价值（过窄 stub/内容已被其他页覆盖）→ 删除。",
            "- create: 从已有页面的指定章节创建新页面；pages 填源页，target 填新页路径，sections 填要迁移的章节。",
            "- trim: 从页面删除不符合 goal 或明确冗余的指定章节；pages 填原页，sections 填要删除的章节。",
            "",
            "## 纪律",
            "- 有把握才提——相关但不同（讲同一主题的不同方面）不是 merge 对象。不确定时不提。",
            "- 拆分输出为 create + trim 两条操作；trim 的 depends_on 必须指向 create，使用同一个 group_id。",
            f"- **最多输出 {_PROPOSE_MAX_COUNT} 条**——只提最可疑的，"
            "不是做全库盘点。写不完会截断，宁少勿多。",
            "",
            "## 全库页面索引",
            overview,
            "",
            "## 输出（纯 JSON 数组，不要 ``` 包裹）",
            '[{"id": "op_1", "op": "merge", "pages": ["concepts/a", "concepts/b"], "reason": "..."},',
            ' {"id": "op_2", "op": "create", "pages": ["concepts/a"], "target": "concepts/new", "sections": ["新主题"], "reason": "..."},',
            ' {"id": "op_3", "op": "trim", "pages": ["concepts/a"], "sections": ["新主题"], "depends_on": ["op_2"], "group_id": "split_1", "reason": "..."}]',
        ]
    )
    try:
        response = await async_invoke_with_retry(
            llm,
            [Message(role="system", content=prompt)],
            max_tokens=_PROPOSE_MAX_TOKENS,
            check=_check_propose_list,
            temperature=0,  # 召回任务——确定性输出，宁稳勿创
            extra_body=_NO_THINKING,
            max_retries=2,
        )
    except Exception as e:
        # LLM 失败不静默——分类后进事件流，返回空（结构健康是最安全的降级）
        err = translate_generic_error(e, context="restructure propose")
        logger.error("  粗提失败: %s", str(err)[:200])
        emit_event("restructure_propose_failed", error=str(err), cause=type(e).__name__)
        return []
    data = _safe_parse_json(response.content)
    if data is None:
        # check 已通过但内容仍坏（截断残余）——降级为无提议，不崩
        emit_event("restructure_propose_failed", error="JSON 二次解析失败", cause="truncated")
        return []
    proposals = [
        Proposal(
            op=item["op"],
            pages=item["pages"],
            target=item.get("target", ""),
            reason=item["reason"],
            id=item.get("id", f"op_{i}"),
            group_id=item.get("group_id", ""),
            depends_on=item.get("depends_on", []),
            sections=item.get("sections", []),
            title=item.get("title", ""),
            summary=item.get("summary", ""),
            goal=item.get("goal", ""),
        )
        for i, item in enumerate(data[:_PROPOSE_MAX_COUNT], 1)
    ]
    emit_event("restructure_proposed", count=len(proposals), ops=[p.op for p in proposals])
    return proposals
