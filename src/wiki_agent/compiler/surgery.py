"""结构手术——LLM 粗提 → 精选复判 → 依赖消解 → 人工确认 → 原子执行。

设计:
    propose_from_index(LLM, index 全量可见)     → 粗提原子操作（merge/delete）
    recheck(LLM, 每条带页面全文 + 引用证据)      → 精选复判（确认/否决/调整方向）
    resolve_conflicts(代码) + re_arbitrate(LLM) → 依赖消解，无法裁决记录不阻塞
    confirm(人, 逐条 yes/no)                    → 唯一闸门
    execute(代码, 顺序无关)                     → 备份 → 原子动作 → 结构化结果

核心约束:
- **提议原子化**——每条提议是不可再分的页级动作；复杂意图（拆分）是
  两个原子提议的组合（create + trim，后续扩展）
- **容量解法**——粗提只见 index（紧凑）；复判只见涉事页面（1-3 页全文）。
  全库视野 + 局部上下文，没有"全部页面进一个 prompt"的问题
- **代码不做相似度阈值**——代码只收集证据（引用计数等），裁决全给 LLM
- **合并即吸收**——被并页正文确定性拼接进目标页，不做 LLM 语义融合
- **矛盾消解是代码职责**——LLM 输出独立提议互不知情，冲突由代码归一化
- **破坏性操作先备份**——受影响文件在执行前复制到 run 容器的 backup/
- **事件全链路**——propose/recheck/conflict/execute/failure 进事件流，
  机器可查每一步；LLM 失败显式分类，不静默吞
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from wiki_agent.compiler.normalize import extract_related
from wiki_agent.compiler.parse import _strip_fence
from wiki_agent.errors import translate_generic_error
from wiki_agent.log import emit_event, get_logger
from wiki_agent.message import Message
from wiki_agent.llm.retry import async_invoke_with_retry

logger = get_logger("SURGERY")

_CONTENT_DIRS = ("concepts", "entities", "topics")
_NO_THINKING = {"thinking": {"type": "disabled"}}

# ── 手术超参数（暂定值——精细调参时统一校准，勿散落魔法数字）──────
_PROPOSE_MAX_TOKENS = 8_000    # 粗提输出预算（130 页 index 实测 2000 截断）
_PROPOSE_MAX_COUNT = 12        # 粗提单次输出上限——召回不是枚举，只提最可疑的
_RECHECK_MAX_TOKENS = 800      # 复判输出预算（确认/否决 + 方向 + 理由）
_RE_ARBITRATE_MAX_TOKENS = 1_000  # 复裁输出预算（冲突清单重提议）
_RECHECK_BODY_CHARS = 2_000    # 复判时给 LLM 的页面正文截断长度


@dataclass
class Proposal:
    """一条原子提议。"""

    op: str                    # merge/delete（粗提）→ merge_into_* / delete / keep（复判后）
    pages: list[str]           # 涉及的页面 slug（不含 .md）
    target: str = ""           # merge 的吸收方向目标页（复判阶段定）
    reason: str = ""


@dataclass
class SurgeryResult:
    """一次执行的结构化结果——机器可消费（事件流之外的人工可读汇总）。"""

    actions: list[str]         # 实际执行的动作描述
    skipped: list[str]         # 因页面不存在而跳过的动作
    backed_up: list[str]       # 备份的文件路径（相对 backup 目录）


# ════════════════════════════════════════════════════════════
#  工具——页面读取 / 证据收集（代码只做这些，不做相似度阈值）
# ════════════════════════════════════════════════════════════

def _load_pages(wiki_dir: str | Path) -> dict[str, dict]:
    """读全库页面。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        {slug: {path, summary, body, related, title}} 映射。
    """
    from wiki_agent.compiler.parse import split_frontmatter

    wiki = Path(wiki_dir)
    pages: dict[str, dict] = {}
    for sub in _CONTENT_DIRS:
        d = wiki / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            slug = str(p.relative_to(wiki)).replace(".md", "")
            content = p.read_text(encoding="utf-8")
            fm, body = split_frontmatter(content)
            pages[slug] = {
                "path": p,
                "title": fm.get("title", slug),
                "summary": fm.get("summary", ""),
                "body": body.strip(),  # parse 层不 strip，调用方按需
                "related": fm.get("related", ""),
            }
    return pages


def _filter_valid_pages(
    proposals: list[Proposal], pages: dict[str, dict],
) -> list[Proposal]:
    """过滤 LLM 幻觉 slug——所有涉及页面必须真实存在，否则丢弃提议。

    Args:
        proposals: 原始提议列表。
        pages: 页面表。

    Returns:
        只含真实页面的提议。
    """
    valid: list[Proposal] = []
    for p in proposals:
        missing = [s for s in p.pages if s not in pages]
        if missing:
            logger.warning("  ✗ 提议引用不存在的页面 %s——丢弃", missing)
            emit_event("surgery_invalid_slug", pages=p.pages,
                       missing=missing, op=p.op)
            continue
        valid.append(p)
    return valid


def _index_overview(wiki_dir: str | Path) -> str:
    """全库紧凑视野——slug + title + summary（粗提的输入）。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        逐行索引文本。
    """
    pages = _load_pages(wiki_dir)
    lines = []
    for slug in sorted(pages):
        p = pages[slug]
        lines.append(f"- [[{slug}]] — {p['title']}"
                     f"{' — ' + p['summary'] if p['summary'] else ''}")
    return "\n".join(lines)


def _incoming_links(pages: dict[str, dict], slug: str) -> list[str]:
    """收集谁引用了 slug——复判的证据（代码收集，LLM 裁决）。

    Args:
        pages: 页面表。
        slug: 被引用页面。

    Returns:
        引用方 slug 列表。
    """
    incoming = []
    for other, page in pages.items():
        if other == slug:
            continue
        if f"[[{slug}]]" in page["body"] or f"[[{slug}|" in page["body"]:
            incoming.append(other)
    return incoming


def _safe_parse_json(content: str):
    """剥 fence + loads——失败返回 None（不崩）。

    retry 的契约是"返回最后一次响应（即使校验未通过）"——
    check 通过与否，解析都可能拿到坏内容（超长截断/重试穷尽）。
    二次校验防护是每个 LLM 调用点的义务（plan 静默失败同款 bug 教训）。

    Args:
        content: LLM 原始输出。

    Returns:
        解析后的 JSON；解析失败返回 None。
    """
    # fence 剥离统一走 parse._strip_fence（含 I5 尾部括号 repair）
    cleaned = _strip_fence(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("  JSON 二次解析失败（check 已通过但内容仍坏）: %s", str(e)[:120])
        return None


# ════════════════════════════════════════════════════════════
#  第一阶段——粗提（LLM 见全库 index，提原子操作）
# ════════════════════════════════════════════════════════════

def _check_propose_list(content: str) -> tuple[bool, str]:
    """校验粗提输出——原子提议数组。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 错误消息)。
    """
    import json as _json
    # fence 剥离统一走 parse._strip_fence（与 check/parse 层同一规约，
    # 含 I5 尾部括号 repair——手术的 LLM 输出同样可能缺尾括号）
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
        if item.get("op") not in ("merge", "delete"):
            return False, f"[{i}].op 必须是 merge/delete，当前: {item.get('op')!r}"
        if not isinstance(item.get("pages", []), list) or not item["pages"]:
            return False, f"[{i}].pages 必须是非空数组"
        if not str(item.get("reason", "")).strip():
            return False, f"[{i}].reason 不能为空"
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
    prompt = "\n\n".join([
        "你是知识库的结构审查员。基于全库页面索引，提出结构手术的原子提议。",
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
        "",
        "## 纪律",
        "- 有把握才提——相关但不同（讲同一主题的不同方面）不是 merge 对象。"
        "不确定时不提。",
        "- 拆分等复杂操作不属于你的职责。",
        f"- **最多输出 {_PROPOSE_MAX_COUNT} 条**——只提最可疑的，"
        "不是做全库盘点。写不完会截断，宁少勿多。",
        "",
        "## 全库页面索引",
        overview,
        "",
        '## 输出（纯 JSON 数组，不要 ``` 包裹）',
        '[{"op": "merge", "pages": ["concepts/a", "concepts/b"], "reason": "..."},',
        ' {"op": "delete", "pages": ["concepts/c"], "reason": "..."}]',
    ])
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
        err = translate_generic_error(e, context="surgery propose")
        logger.error("  粗提失败: %s", str(err)[:200])
        emit_event("surgery_propose_failed", error=str(err),
                   cause=type(e).__name__)
        return []
    data = _safe_parse_json(response.content)
    if data is None:
        # check 已通过但内容仍坏（截断残余）——降级为无提议，不崩
        emit_event("surgery_propose_failed", error="JSON 二次解析失败",
                   cause="truncated")
        return []
    proposals = [
        Proposal(op=item["op"], pages=item["pages"], reason=item["reason"])
        for item in data[:_PROPOSE_MAX_COUNT]
    ]
    emit_event("surgery_proposed", count=len(proposals),
               ops=[p.op for p in proposals])
    return proposals


# ════════════════════════════════════════════════════════════
#  第二阶段——精选复判（每条带页面全文 + 引用证据，单独复检）
# ════════════════════════════════════════════════════════════

def _check_recheck(content: str) -> tuple[bool, str]:
    """校验复判输出——verdict 字段。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 错误消息)。
    """
    import json as _json
    cleaned = _strip_fence(content)
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
        return "\n\n".join([
            "你是知识库的结构复判员。复核一条合并提议——看全文，不看摘要。",
            "## 提议",
            f"merge: [[{a}]] ↔ [[{b}]]（粗提理由: {prop.reason}）",
            f"## [[{a}]]\n{pages[a]['summary']}\n\n{pages[a]['body'][:_RECHECK_BODY_CHARS]}",
            f"## [[{b}]]\n{pages[b]['summary']}\n\n{pages[b]['body'][:_RECHECK_BODY_CHARS]}",
            "## 复判",
            "1. 两页是否真的高度重叠（合并后信息基本无损）？",
            "2. 若合并，谁吸收谁（内容更全/更系统的一页做目标）？",
            '## 输出（纯 JSON）: {"verdict": "confirm"|"reject", '
            '"op": "merge_into_first"|"merge_into_second"|"keep_both", "reason": "..."}',
            "verdict=reject 时 op 填 keep_both。",
        ])
    # delete
    slug = prop.pages[0]
    incoming = _incoming_links(pages, slug)
    return "\n\n".join([
        "你是知识库的结构复判员。复核一条删除提议——看全文和引用证据。",
        "## 提议",
        f"delete: [[{slug}]]（粗提理由: {prop.reason}）",
        f"## [[{slug}]]\n{pages[slug]['summary']}\n\n{pages[slug]['body'][:_RECHECK_BODY_CHARS]}",
        f"## 引用证据\n{len(incoming)} 个页面链接到它: {incoming[:10]}",
        "## 复判",
        "1. 该页内容是否已被其他页面覆盖（删除无信息损失）？",
        "2. 引用它的页面会因删除而死链——信息是否真的可弃？",
        '## 输出（纯 JSON）: {"verdict": "confirm"|"reject", '
        '"op": "delete"|"keep", "reason": "..."}',
    ])


async def recheck(
    llm, wiki_dir: str | Path, proposals: list[Proposal],
) -> tuple[list[Proposal], list[tuple[Proposal, str]]]:
    """精选复判——每条提议单独复检（只带涉事页面，局部上下文）。

    Args:
        llm: LLM 客户端。
        wiki_dir: wiki 根目录。
        proposals: 待复判提议。

    Returns:
        (confirmed, rejected)——rejected 携带否决理由，调用方决定怎么呈现。
    """
    pages = _load_pages(wiki_dir)
    # 幻觉 slug 防线——提议页面必须存在，否则复判读文件会 KeyError
    proposals = _filter_valid_pages(proposals, pages)
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
                max_tokens=_RECHECK_MAX_TOKENS,
                check=_check_recheck,
                temperature=0,  # 裁决任务——确定性判定
                extra_body=_NO_THINKING,
                max_retries=2,
            )
        except Exception as e:
            err = translate_generic_error(e, context="surgery recheck")
            logger.error("  复判失败 %s: %s", prop.pages, str(err)[:200])
            emit_event("surgery_recheck_failed", pages=prop.pages,
                       error=str(err), cause=type(e).__name__)
            continue
        data = _safe_parse_json(response.content)
        if data is None:
            emit_event("surgery_recheck_failed", pages=prop.pages,
                       error="JSON 二次解析失败", cause="truncated")
            continue
        if data["verdict"] != "confirm":
            reason = data.get("reason", "")
            logger.info("  - 复判否决: %s（%s）", prop.pages, reason)
            emit_event("surgery_rejected", pages=prop.pages, reason=reason)
            rejected.append((prop, reason))
            continue
        op = data["op"]
        target = ""
        if op == "merge_into_first":
            target = prop.pages[0]
        elif op == "merge_into_second":
            target = prop.pages[1]
        confirmed.append(Proposal(
            op=op, pages=prop.pages, target=target, reason=data["reason"],
        ))
        emit_event("surgery_confirmed", op=op, pages=prop.pages, target=target)
    return confirmed, rejected


# ════════════════════════════════════════════════════════════
#  依赖分析 + 冲突消解——复判之后、确认之前
# ════════════════════════════════════════════════════════════

@dataclass
class Conflict:
    """一组无法确定性消解的提议冲突——交给 LLM 复裁或人工。"""

    kind: str                  # "opposite_direction" / "delete_vs_merge"
    proposals: list[Proposal]
    detail: str = ""


def _page_quality(page: dict) -> int:
    """页面质量分——冲突时定 merge 方向。

    Args:
        page: 页面数据。

    Returns:
        质量分（正文长度 + 2×摘要长度）。
    """
    return len(page["body"]) + 2 * len(page["summary"])


def _src_of(prop: Proposal) -> str:
    """返回 merge 的被吸收页。

    merge_into_first 吸收 pages[1]，反之 pages[0]。

    Args:
        prop: merge 提议。

    Returns:
        被吸收页 slug。
    """
    return prop.pages[1] if prop.op == "merge_into_first" else prop.pages[0]


def resolve_conflicts(
    proposals: list[Proposal], pages: dict[str, dict],
) -> tuple[list[Proposal], list[Conflict]]:
    """提议间一致性分析——确定性消解 + 冲突收集。

    消解规则（按信息优先排序）:
    1. 完全重复的提议 → 去重
    2. 同一对页面的双向 merge → 质量高的一页做吸收方，另一条丢弃
    3. delete 被吸收页 → merge 赢（merge 保留信息，delete 只减不增）
    4. delete 吸收方 → Conflict（吸收方即将消失，LLM 复裁）
    5. merge 链（A→B 且 B→C）→ 合法保留（每步原子，内容沿链流动）

    Args:
        proposals: 复判后的提议。
        pages: 页面表（质量分用）。

    Returns:
        (clean, conflicts)——clean 直接进执行，conflicts 待复裁。
    """
    # 1. 去重
    seen: set[tuple] = set()
    unique: list[Proposal] = []
    for p in proposals:
        key = (p.op, tuple(sorted(p.pages)), p.target)
        if key in seen:
            logger.info("  去重: %s", p.pages)
            continue
        seen.add(key)
        unique.append(p)

    # 索引: merge 按无序对，delete 按单页
    merges: dict[tuple, list[Proposal]] = {}
    deletes: dict[str, Proposal] = {}
    for p in unique:
        if p.op.startswith("merge"):
            merges.setdefault(tuple(sorted(p.pages)), []).append(p)
        elif p.op == "delete":
            deletes[p.pages[0]] = p

    clean: list[Proposal] = []
    conflicts: list[Conflict] = []

    # 2. 双向 merge → 质量定方向
    for pair, group in merges.items():
        if len(group) == 1:
            clean.append(group[0])
            continue
        best = max(group, key=lambda p: _page_quality(pages[p.target]))
        logger.info("  方向互斥消解: %s ↔ %s → 吸收方 %s（质量分定）",
                    pair[0], pair[1], best.target)
        clean.append(best)

    # 3/4. delete vs merge 撞页
    for m in clean:
        if not m.op.startswith("merge"):
            continue
        src = _src_of(m)
        # 3. delete 被吸收页 → merge 赢
        if src in deletes:
            logger.info("  merge 覆盖 delete: %s（将被 %s 吸收）", src, m.target)
            del deletes[src]
        # 4. delete 吸收方 → 冲突
        if m.target in deletes:
            conflicts.append(Conflict(
                kind="delete_vs_merge",
                proposals=[m, deletes[m.target]],
                detail=f"{m.target} 既是 merge 吸收方又被提议删除",
            ))

    clean.extend(deletes.values())
    if conflicts:
        emit_event("surgery_conflict", count=len(conflicts),
                   kinds=[c.kind for c in conflicts])
    return clean, conflicts


# ════════════════════════════════════════════════════════════
#  LLM 复裁（callback retry）——冲突反馈回 LLM 重裁决
# ════════════════════════════════════════════════════════════

def _check_re_arbitrate(content: str) -> tuple[bool, str]:
    """校验复裁输出——提议数组。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 错误消息)。
    """
    import json as _json
    cleaned = _strip_fence(content)
    try:
        data = _json.loads(cleaned)
    except _json.JSONDecodeError as e:
        return False, f"JSON 格式错误: {e}"
    if not isinstance(data, list):
        return False, "输出必须是数组（放弃全部冲突时输出 []）"
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("pages", []), list):
            return False, f"[{i}] 格式错误"
        if item.get("op") not in ("merge", "delete"):
            return False, f"[{i}].op 非法: {item.get('op')!r}"
    return True, ""


@dataclass
class ArbitrationResult:
    """复裁结果——resolved 进执行，unresolved 记录待决策（不阻塞）。

    unresolved 的归宿是统一的决策队列（TODO）——与 ingest 失败重试
    等"需要用户抉择"的项同一条通道，用户在合适的时机统一处理。
    """

    resolved: list[Proposal]
    unresolved: list[Conflict]


async def re_arbitrate(
    llm, wiki_dir: str | Path, conflicts: list[Conflict],
) -> ArbitrationResult:
    """冲突复裁——把冲突反馈给 LLM，让它重提议（callback retry）。

    无法仲裁的冲突**不静默丢弃、不自动推进**——返回 unresolved，
    由调用方阻塞等人（手术是破坏性低频操作，半应用状态比等待更糟）。

    Args:
        llm: LLM 客户端。
        wiki_dir: wiki 根目录。
        conflicts: 待复裁冲突。

    Returns:
        ArbitrationResult（resolved 进执行 / unresolved 记录待决策）。
    """
    pages = _load_pages(wiki_dir)
    conflict_text = "\n\n".join(
        f"- {c.kind}: {c.detail}\n"
        + "\n".join(
            f"  [{p.op}] {p.pages} → {p.target}（{p.reason[:80]}）"
            for p in c.proposals
        )
        for c in conflicts
    )
    prompt = "\n\n".join([
        "你是知识库的结构复裁员。以下提议互相冲突，重新裁决。",
        "",
        "## 冲突清单",
        conflict_text,
        "",
        "## 规则",
        "- 每组冲突只保留最多一条有效提议（可改方向，merge 的 pages 保持两个 slug）",
        "- 无法裁决的组（信息不足/各有道理）在 reason 里写明，op 用 \"unresolved\"",
        "- 每条输出: {\"op\": \"merge\"|\"delete\"|\"unresolved\", \"pages\": [...], "
        "\"reason\": \"...\"}（merge 的 pages 第一个是吸收方）",
        "",
        '## 输出（纯 JSON 数组，不要 ``` 包裹）',
    ])
    try:
        response = await async_invoke_with_retry(
            llm,
            [Message(role="system", content=prompt)],
            max_tokens=_RE_ARBITRATE_MAX_TOKENS,
            check=_check_re_arbitrate,
            extra_body=_NO_THINKING,
            temperature=0,
            max_retries=2,
        )
    except Exception as e:
        # LLM 都仲裁不了 = 复裁失败——全部记录，不阻塞
        logger.error("  复裁失败: %s——%d 组冲突记录待决策", str(e)[:120], len(conflicts))
        return ArbitrationResult(resolved=[], unresolved=list(conflicts))
    data = _safe_parse_json(response.content)
    if data is None:
        logger.error("  复裁 JSON 二次解析失败——全部冲突记录待决策")
        return ArbitrationResult(resolved=[], unresolved=list(conflicts))
    resolved: list[Proposal] = []
    unresolved: list[Conflict] = list(conflicts)
    for item in data:
        if item.get("op") == "unresolved":
            continue  # 留在 unresolved 里
        op = item["op"]
        pages_ = item["pages"]
        target = pages_[0] if op == "merge" else ""
        resolved.append(Proposal(op=op, pages=pages_, target=target,
                                 reason=item.get("reason", "")))
    # 有 resolved 输出时，对应冲突视为已消解——按涉及页匹配清掉
    resolved_pages = {p for pr in resolved for p in pr.pages}
    still_unresolved = [
        c for c in conflicts
        if not (resolved_pages & {p for pr in c.proposals for p in pr.pages})
    ]
    return ArbitrationResult(resolved=resolved, unresolved=still_unresolved)


# ════════════════════════════════════════════════════════════
#  execute——原子动作，内存算最终状态后一次落盘
# ════════════════════════════════════════════════════════════

def _rewrite_links(pages: dict[str, dict], old_slug: str, new_slug: str) -> None:
    """全库把 [[old]] 链接重写为 [[new]]（含别名形式）。

    Args:
        pages: 页面表。
        old_slug: 旧 slug。
        new_slug: 新 slug。
    """
    pattern = re.compile(rf"\[\[{re.escape(old_slug)}(?:\|([^\]]+?))?\]\]")
    for page in pages.values():
        p = page["path"]
        content = p.read_text(encoding="utf-8")
        new_content = pattern.sub(
            lambda m: f"[[{new_slug}|{m.group(1)}]]" if m.group(1) else f"[[{new_slug}]]",
            content,
        )
        if new_content != content:
            p.write_text(new_content, encoding="utf-8")
            logger.info("  链接重写: %s 中 [[%s]] → [[%s]]", p.name, old_slug, new_slug)


def _remove_index_entry(wiki: Path, slug: str) -> None:
    index_path = wiki / "index.md"
    if not index_path.exists():
        return
    lines = [l for l in index_path.read_text(encoding="utf-8").split("\n")
             if f"[[{slug}]]" not in l]
    index_path.write_text("\n".join(lines), encoding="utf-8")


_LINK_RE = re.compile(r"\[\[([^\]]+?)(?:\|([^\]]+?))?\]\]")


def _rewrite_source_links(text: str, source_slug: str, target_slug: str) -> str:
    """源页正文内的自引用 → 指向合并后的目标页（内容搬家，链接跟着搬）。

    Args:
        text: 页面正文。
        source_slug: 源页 slug。
        target_slug: 目标页 slug。

    Returns:
        重写后的文本。
    """
    return _LINK_RE.sub(
        lambda m: (
            f"[[{target_slug}|{m.group(2)}]]" if m.group(1).strip() == source_slug and m.group(2)
            else f"[[{target_slug}]]" if m.group(1).strip() == source_slug
            else m.group(0)
        ),
        text,
    )


def _plain_source_links(text: str, source_slug: str, source_title: str) -> str:
    """目标页原正文对源页的引用 → 纯文本（合并后成了自链，转别名）。

    Args:
        text: 页面正文。
        source_slug: 源页 slug。
        source_title: 源页标题（别名兜底）。

    Returns:
        重写后的文本。
    """
    return _LINK_RE.sub(
        lambda m: (
            m.group(2) if m.group(1).strip() == source_slug and m.group(2)
            else source_title if m.group(1).strip() == source_slug
            else m.group(0)
        ),
        text,
    )


def execute_merge(wiki: Path, prop: Proposal) -> None:
    """合并吸收——原子: B 正文拼接进 A + 链接重写 + index 清理 + 删 B。

    三类引用三种处理（bug 教训——顺序和分类都是领域知识）:
    - 源页正文内的自引用 → [[target]]（内容搬家，链接跟着搬）
    - 目标页原正文对源页的引用 → 纯文本别名（合并后自链无意义）
    - 其他页对源页的引用 → [[target]]（保留别名）
    吸收章节标题用纯文本（不带 wikilink）——重写器碰不到它。

    Args:
        wiki: wiki 根目录。
        prop: merge 提议（含方向与 target）。
    """
    source_slug = prop.pages[1] if prop.op == "merge_into_first" \
        else prop.pages[0]
    target_slug = prop.target
    pages = _load_pages(wiki)
    target = pages[target_slug]
    source = pages[source_slug]

    # 1. 拼接（三处各自处理引用，然后写盘）
    source_body = _rewrite_source_links(source["body"], source_slug, target_slug)
    target_orig = target["path"].read_text(encoding="utf-8").rstrip()
    target_orig = _plain_source_links(target_orig, source_slug, source["title"])
    merged = (
        f"{target_orig}\n\n"
        f"## 合并自 {source['title']} 的内容\n\n"
        f"{source_body}\n"
    )
    # related 由代码重推（extract_related 与 normalize 同源逻辑）
    valid = {s for s in pages if s != source_slug}
    merged = extract_related(merged, valid_slugs=valid)
    target["path"].write_text(merged, encoding="utf-8")
    logger.info("  ✓ 合并: %s 吸收 %s", target_slug, source_slug)

    # 2. 其他页链接重写 → [[target]]
    for slug, page in pages.items():
        if slug in (target_slug, source_slug):
            continue
        p = page["path"]
        content = p.read_text(encoding="utf-8")
        new_content = _rewrite_source_links(content, source_slug, target_slug)
        if new_content != content:
            p.write_text(new_content, encoding="utf-8")
            logger.info("  链接重写: %s 中 [[%s]] → [[%s]]", p.name, source_slug, target_slug)

    # 3. index 清理 + 删除源页
    _remove_index_entry(wiki, source_slug)
    source["path"].unlink()
    logger.info("  ✓ 删除: %s（已合并进 %s）", source_slug, target_slug)


def execute_delete(wiki: Path, prop: Proposal) -> None:
    """纯删除——文件 + index + 引用它的链接转纯文本。

    Args:
        wiki: wiki 根目录。
        prop: delete 提议。
    """
    slug = prop.pages[0]
    pages = _load_pages(wiki)
    # 引用者链接转纯文本（死链防线同款语义）
    pattern = re.compile(rf"\[\[{re.escape(slug)}(?:\|([^\]]+?))?\]\]")
    for page in pages.values():
        p = page["path"]
        content = p.read_text(encoding="utf-8")
        new_content = pattern.sub(
            lambda m: m.group(1) if m.group(1) else slug.rsplit("/", 1)[-1],
            content,
        )
        if new_content != content:
            p.write_text(new_content, encoding="utf-8")
    _remove_index_entry(wiki, slug)
    (wiki / f"{slug}.md").unlink()
    logger.info("  ✓ 删除: %s", slug)


def _backup(wiki: Path, pages: dict[str, dict], slugs: set[str],
            backup_dir: Path | None) -> list[str]:
    """执行前备份受影响文件 + index——破坏性操作可回滚。

    Args:
        wiki: wiki 根目录。
        pages: 页面表。
        slugs: 受影响页面集合。
        backup_dir: 备份目录（None 跳过备份）。

    Returns:
        备份的文件相对路径列表。
    """
    if backup_dir is None:
        return []
    backup_dir.mkdir(parents=True, exist_ok=True)
    backed: list[str] = []
    for slug in sorted(slugs):
        p = pages[slug]["path"]
        rel = p.relative_to(wiki)
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        backed.append(str(rel))
    index_path = wiki / "index.md"
    if index_path.exists():
        shutil.copy2(index_path, backup_dir / "index.md")
        backed.append("index.md")
    logger.info("  备份 %d 个文件 → %s", len(backed), backup_dir)
    return backed


def execute(
    wiki_dir: str | Path,
    proposals: list[Proposal],
    *,
    backup_dir: str | Path | None = None,
) -> SurgeryResult:
    """执行全部复判通过的提议——先备份，再逐个原子动作。

    - 状态复核: 执行前重读页面表，页面已不存在的动作跳过（记 skipped），
      不 KeyError 不误删
    - 备份: backup_dir 提供时复制受影响文件 + index（破坏性操作可回滚）
    - 结果结构化: SurgeryResult（actions/skipped/backed_up）——机器可消费

    Args:
        wiki_dir: wiki 根目录。
        proposals: 复判通过的提议。
        backup_dir: 备份目录（None 跳过备份）。

    Returns:
        结构化执行结果。
    """
    wiki = Path(wiki_dir)
    pages = _load_pages(wiki)
    result = SurgeryResult(actions=[], skipped=[], backed_up=[])

    # 受影响页面集合 → 备份
    touched = {s for p in proposals for s in p.pages if s in pages}
    if backup_dir is not None:
        result.backed_up = _backup(wiki, pages, touched, Path(backup_dir))

    for prop in proposals:
        if prop.op in ("keep_both", "keep"):
            logger.info("  - 保留: %s（%s）", prop.pages, prop.reason)
            continue
        # 状态复核——页面必须还在（前面的动作可能已删掉它）
        missing = [s for s in prop.pages if s not in pages]
        if missing:
            logger.warning("  ✗ 跳过 %s: 页面已不存在 %s", prop.pages, missing)
            result.skipped.append(f"{prop.op} {prop.pages}（页面不存在）")
            emit_event("surgery_skipped", op=prop.op, pages=prop.pages,
                       reason="page_missing")
            continue
        try:
            if prop.op.startswith("merge"):
                execute_merge(wiki, prop)
                result.actions.append(f"merge {prop.pages} -> {prop.target}")
                # 更新内存页面表——被吸收页从表里移除，后续动作复核用
                src = _src_of(prop)
                del pages[src]
                emit_event("surgery_merged", pages=prop.pages,
                           target=prop.target)
            elif prop.op == "delete":
                execute_delete(wiki, prop)
                result.actions.append(f"delete {prop.pages[0]}")
                del pages[prop.pages[0]]
                emit_event("surgery_deleted", pages=prop.pages)
        except Exception as e:
            err = translate_generic_error(e, context="surgery execute")
            logger.error("  ✗ 执行失败 %s: %s", prop.pages, str(err)[:200])
            result.skipped.append(f"{prop.op} {prop.pages}（执行失败: {err}）")
            emit_event("surgery_execute_failed", op=prop.op,
                       pages=prop.pages, error=str(err),
                       cause=type(e).__name__)

    return result
