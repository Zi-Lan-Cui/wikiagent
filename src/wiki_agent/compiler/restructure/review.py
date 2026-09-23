"""结构重组的 LLM 复判与冲突复裁——每条带页面全文 + 引用证据，单独复检。

LLM 只出现在提议与复判两阶段（蓝图约束）。复判确认方向、复裁消解冲突；
无法裁决的冲突不静默丢弃——返回 unresolved 交调用方。
"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.integration.parse import strip_fence
from wiki_agent.compiler.models import JSON_MODE, NO_THINKING
from wiki_agent.compiler.restructure.common import (
    coerce_list,
    filter_valid_pages,
    incoming_links,
    load_pages,
    safe_parse_json,
)
from wiki_agent.compiler.restructure.models import (
    RE_ARBITRATE_MAX_TOKENS,
    RECHECK_BODY_CHARS,
    RECHECK_MAX_TOKENS,
    ArbitrationResult,
    Conflict,
    Proposal,
)
from wiki_agent.conversation import Message
from wiki_agent.errors import translate_generic_error
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import emit_event, get_logger

logger = get_logger("RESTRUCTURE")


def _check_recheck(content: str) -> tuple[bool, str]:
    """校验复判输出——verdict 字段。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 错误消息)。
    """
    import json as _json

    cleaned = strip_fence(content)
    try:
        data = _json.loads(cleaned)
    except _json.JSONDecodeError as e:
        return False, f"JSON 格式错误: {e}"
    if data.get("verdict") not in ("confirm", "reject"):
        return False, f"verdict 必须是 confirm/reject，当前: {data.get('verdict')!r}"
    return True, ""


def _recheck_prompt(prop: Proposal, pages: dict[str, dict]) -> str:
    """构造复判 prompt——merge/delete 各自带涉事页面与证据。

    Args:
        prop: 待复判提议。
        pages: 页面表。

    Returns:
        system prompt 文本。
    """
    if prop.op == "merge":
        a, b = prop.pages
        return "\n\n".join(
            [
                "你是知识库的结构复判员。复核一条合并提议——看全文，不看摘要。",
                "## 提议",
                f"merge: [[{a}]] ↔ [[{b}]]（粗提理由: {prop.reason}）",
                f"## [[{a}]]\n{pages[a]['summary']}\n\n{pages[a]['body'][:RECHECK_BODY_CHARS]}",
                f"## [[{b}]]\n{pages[b]['summary']}\n\n{pages[b]['body'][:RECHECK_BODY_CHARS]}",
                "## 复判",
                "1. 两页是否真的高度重叠（合并后信息基本无损）？",
                "2. 若合并，谁吸收谁（内容更全/更系统的一页做目标）？",
                '## 输出 JSON 对象: {"verdict": "confirm"|"reject", '
                '"op": "merge_into_first"|"merge_into_second"|"keep_both", "reason": "..."}',
                "verdict=reject 时 op 填 keep_both。",
            ]
        )
    if prop.op in ("create", "trim"):
        slug = prop.pages[0]
        action = "创建新页" if prop.op == "create" else "从原页移除章节"
        target = f"，目标页为 [[{prop.target}]]" if prop.target else ""
        return "\n\n".join(
            [
                "你是知识库的结构复判员。复核一条内容迁移提议——只依据页面全文。",
                "## 提议",
                f"{prop.op}: [[{slug}]]{target}（粗提理由: {prop.reason}）",
                f"操作含义：{action}。指定章节：{prop.sections}",
                f"## [[{slug}]]\n{pages[slug]['summary']}\n\n{pages[slug]['body'][:RECHECK_BODY_CHARS]}",
                "## 复判",
                "确认章节边界确实存在，且操作不会丢失原页主题的必要信息。",
                '## 输出 JSON 对象: {"verdict": "confirm"|"reject", '
                f'"op": "{prop.op}", "sections": ["章节名"], "reason": "..."',
                (', "target": "目标页路径"' if prop.op == "create" else "") + "}",
            ]
        )
    # delete
    slug = prop.pages[0]
    incoming = incoming_links(pages, slug)
    return "\n\n".join(
        [
            "你是知识库的结构复判员。复核一条删除提议——看全文和引用证据。",
            "## 提议",
            f"delete: [[{slug}]]（粗提理由: {prop.reason}）",
            f"## [[{slug}]]\n{pages[slug]['summary']}\n\n{pages[slug]['body'][:RECHECK_BODY_CHARS]}",
            f"## 引用证据\n{len(incoming)} 个页面链接到它: {incoming[:10]}",
            "## 复判",
            "1. 该页内容是否已被其他页面覆盖（删除无信息损失）？",
            "2. 引用它的页面会因删除而死链——信息是否真的可弃？",
            '## 输出 JSON 对象: {"verdict": "confirm"|"reject", '
            '"op": "delete"|"keep", "reason": "..."}',
        ]
    )


async def recheck(
    llm,
    wiki_dir: str | Path,
    proposals: list[Proposal],
) -> tuple[list[Proposal], list[tuple[Proposal, str]]]:
    """精选复判——每条提议单独复检（只带涉事页面，局部上下文）。

    Args:
        llm: LLM 客户端。
        wiki_dir: wiki 根目录。
        proposals: 待复判提议。

    Returns:
        (confirmed, rejected)——rejected 携带否决理由，调用方决定怎么呈现。
    """
    pages = load_pages(wiki_dir)
    # 幻觉 slug 防线——提议页面必须存在，否则复判读文件会 KeyError
    proposals = filter_valid_pages(proposals, pages)
    confirmed: list[Proposal] = []
    rejected: list[tuple[Proposal, str]] = []
    for prop in proposals:
        if prop.op == "merge" and len(prop.pages) != 2:
            logger.warning("  ✗ 跳过非法 merge 提议: %s", prop.pages)
            continue
        try:
            response = await async_invoke_with_retry(
                llm,
                [Message(role="system", content=_recheck_prompt(prop, pages))],
                max_tokens=RECHECK_MAX_TOKENS,
                check=_check_recheck,
                temperature=0,  # 裁决任务——确定性判定
                extra_body=NO_THINKING,
                max_attempts=2,
                response_format=JSON_MODE,
            )
        except Exception as e:
            err = translate_generic_error(e, context="restructure recheck")
            logger.error("  复判失败 %s: %s", prop.pages, str(err)[:200])
            emit_event(
                "restructure_recheck_failed",
                pages=prop.pages,
                error=str(err),
                cause=type(e).__name__,
            )
            continue
        data = safe_parse_json(response.content)
        if data is None:
            emit_event(
                "restructure_recheck_failed",
                pages=prop.pages,
                error="JSON 二次解析失败",
                cause="truncated",
            )
            continue
        if data["verdict"] != "confirm":
            reason = data.get("reason", "")
            logger.info("  - 复判否决: %s（%s）", prop.pages, reason)
            emit_event("restructure_rejected", pages=prop.pages, reason=reason)
            rejected.append((prop, reason))
            continue
        op = data["op"]
        target = ""
        if op == "merge_into_first":
            target = prop.pages[0]
        elif op == "merge_into_second":
            target = prop.pages[1]
        if op in ("create", "trim"):
            if op != prop.op or not data.get("sections"):
                rejected.append((prop, "复判未返回合法的原子操作字段"))
                continue
            target = data.get("target", prop.target)
            sections = data["sections"]
        else:
            sections = prop.sections
        confirmed.append(
            Proposal(
                op=op,
                pages=prop.pages,
                target=target,
                reason=data["reason"],
                id=prop.id,
                group_id=prop.group_id,
                depends_on=prop.depends_on,
                sections=sections,
                title=prop.title,
                summary=prop.summary,
                goal=prop.goal,
            )
        )
        emit_event("restructure_confirmed", op=op, pages=prop.pages, target=target)
    return confirmed, rejected


# LLM 复裁（callback retry）——冲突反馈回 LLM 重裁决


def _check_re_arbitrate(content: str) -> tuple[bool, str]:
    """校验复裁输出——提议数组。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 错误消息)。
    """
    import json as _json

    cleaned = strip_fence(content)
    try:
        data = _json.loads(cleaned)
    except _json.JSONDecodeError as e:
        return False, f"JSON 格式错误: {e}"
    # 新契约 {"resolutions": [...]}（json_object）；裸数组宽容
    if isinstance(data, dict):
        data = data.get("resolutions")
    if not isinstance(data, list):
        return False, '输出必须是 {"resolutions": [...]}——放弃全部冲突时为空数组'
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("pages", []), list):
            return False, f"[{i}] 格式错误"
        op = item.get("op")
        if op not in ("merge", "delete", "unresolved"):
            return False, f"[{i}].op 非法: {op!r}"
        pages = item.get("pages", [])
        if op == "merge" and (
            len(pages) != 2 or not all(isinstance(p, str) and p for p in pages)
        ):
            return False, f"[{i}] merge 的 pages 必须是两个页面 slug"
        if op == "delete" and not pages:
            return False, f"[{i}] delete 缺少页面"
    return True, ""


async def re_arbitrate(
    llm,
    wiki_dir: str | Path,
    conflicts: list[Conflict],
) -> ArbitrationResult:
    """冲突复裁——把冲突反馈给 LLM，让它重提议（callback retry）。

    无法仲裁的冲突**不静默丢弃、不自动推进**——返回 unresolved，
    由调用方阻塞等人（重组是破坏性低频操作，半应用状态比等待更糟）。

    Args:
        llm: LLM 客户端。
        wiki_dir: wiki 根目录。
        conflicts: 待复裁冲突。

    Returns:
        ArbitrationResult（resolved 进执行 / unresolved 记录待决策）。
    """
    conflict_text = "\n\n".join(
        f"- {c.kind}: {c.detail}\n"
        + "\n".join(f"  [{p.op}] {p.pages} → {p.target}（{p.reason[:80]}）" for p in c.proposals)
        for c in conflicts
    )
    prompt = "\n\n".join(
        [
            "你是知识库的结构复裁员。以下提议互相冲突，重新裁决。",
            "",
            "## 冲突清单",
            conflict_text,
            "",
            "## 规则",
            "- 每组冲突只保留最多一条有效提议（可改方向，merge 的 pages 保持两个 slug）",
            '- 无法裁决的组（信息不足/各有道理）在 reason 里写明，op 用 "unresolved"',
            '- 每条输出: {"op": "merge"|"delete"|"unresolved", "pages": [...], '
            '"reason": "..."}（merge 的 pages 第一个是吸收方）',
            "",
            '## 输出 JSON 对象 {"resolutions": [',
            '  {"op": "merge"|"delete"|"unresolved", "pages": [...], "reason": "..."}]}',
        ]
    )
    try:
        response = await async_invoke_with_retry(
            llm,
            [Message(role="system", content=prompt)],
            max_tokens=RE_ARBITRATE_MAX_TOKENS,
            check=_check_re_arbitrate,
            extra_body=NO_THINKING,
            temperature=0,
            max_attempts=2,
            response_format=JSON_MODE,
        )
    except Exception as e:
        # LLM 都仲裁不了 = 复裁失败——全部记录，不阻塞
        logger.error("  复裁失败: %s——%d 组冲突记录待决策", str(e)[:120], len(conflicts))
        return ArbitrationResult(resolved=[], unresolved=list(conflicts))
    data = coerce_list(safe_parse_json(response.content), "resolutions")
    if data is None:
        logger.error("  复裁 JSON 二次解析失败——全部冲突记录待决策")
        return ArbitrationResult(resolved=[], unresolved=list(conflicts))
    resolved: list[Proposal] = []
    for item in data:
        if item.get("op") == "unresolved":
            continue  # 留在 unresolved 里
        op = item["op"]
        pages_ = item["pages"]
        if op == "merge":
            # 复裁 prompt 约定"pages 第一个是吸收方"——执行侧的对应形式是
            # merge_into_first（吸收 pages[0]、删除 pages[1]）。直接产 op=merge
            # 会让 execute_merge 把 pages[0] 当被删方、与 target 同页，删掉吸收方。
            resolved.append(
                Proposal(
                    op="merge_into_first",
                    pages=pages_,
                    target=pages_[0],
                    reason=item.get("reason", ""),
                )
            )
        else:
            resolved.append(
                Proposal(op=op, pages=pages_, target="", reason=item.get("reason", ""))
            )
    # 有 resolved 输出时，对应冲突视为已消解——按涉及页匹配清掉
    resolved_pages = {p for pr in resolved for p in pr.pages}
    still_unresolved = [
        c for c in conflicts if not (resolved_pages & {p for pr in c.proposals for p in pr.pages})
    ]
    return ArbitrationResult(resolved=resolved, unresolved=still_unresolved)
