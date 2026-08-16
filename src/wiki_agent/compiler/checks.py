"""校验层——检测原子 + 定义"什么算好输出"（check 回调）。

与 parse.py 的分界: 本模块校验，parse 解析。check 返回
(ok, reason)——错误消息可执行（LLM 重试时知道错在哪）。
共享 _strip_fence（parse.py）剥除 fence 噪声。

分层（依赖单向）:
    checks.py    检测原子（fence 状态机/正文提取）+ check 回调——判定逻辑唯一所在
    quality.py   体检（Issue 组装 + scan_wiki 扫描 + 报告，判定复用本模块）
    normalize.py 修内容（fix/inject）+ check_page_quality 定稿兜底
    quality → checks，normalize → quality → checks
"""

from __future__ import annotations

import json
import re

from wiki_agent.compiler.parse import _extract_analyze_parts, split_frontmatter, _strip_fence

_VALID_RELATIONS = {"duplicate", "extends", "related", "contradicts", "unrelated"}
# "重要" 是 LLM 的自然语言高频词（实测 refine 3 次违规全是它）——
# 枚举拦截性价比低，并入合法集
_VALID_IMPORTANCE = {"核心", "边缘", "重要"}
_VALID_DISPOSITIONS = {"new", "update"}

# 编译流水线关 thinking——deepseek-v4-flash 是 reasoning 模型，
# 思考段会静默吃掉整个 max_tokens 预算、content 留空（审计 C1 根因）。
# 编译输出是"写页面"不是"解难题"，直接写更可靠也更便宜。
_NO_THINKING = {"thinking": {"type": "disabled"}}

# ════════════════════════════════════════════════════════════
#  共享检测原子——quality 的 scan 与 normalize 的修复都建在这些原子上
# ════════════════════════════════════════════════════════════

# fence 配对 = 括号匹配（带标记的 ```python 是左括号，裸 ``` 是右括号，
# 且右括号只在块内才有效——块内的 ```python 是内容不翻转）。
# 旧奇数计数假阴性事故: 两个 ```python（2 开 0 闭）被误判"已闭合"，
# anonymous-function.md 标题全被吞进代码块还落了盘。
_FENCE_OPEN = re.compile(r"^```\S+\s*$")  # 带语言标记 = 开（裸 ``` 不匹配）


def _iter_code_runs(content: str):
    """逐行产出 (行, 是否在代码块内)——括号配对式 fence 状态机。

    规则（CommonMark 括号匹配）:
    - ```python 等带语言标记的 fence = 左括号（深度 +1）
    - 裸 ``` 且深度 > 0 = 右括号（深度 -1；块内的裸 ``` 是内容不翻转）
    - 块内的 ```python = 内容，不翻转

    Args:
        content: 页面内容。

    Yields:
        (行文本, 该行是否在代码块内) 二元组——检测闭合与提取
        非代码文本共用。
    """
    depth = 0
    for line in content.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if _FENCE_OPEN.match(stripped):
                # 带标记 = 左括号；块内出现也算内容（保持深度不变）
                if depth == 0:
                    depth = 1
                yield line, True
                continue
            # 裸 ``` = 右括号（块内才有效）
            if depth > 0:
                depth = 0
                yield line, True
                continue
            # 块外裸 ``` 是游离的——按内容行处理
            yield line, False
            continue
        yield line, depth > 0


def iter_text_outside_code(content: str):
    """逐行产出非代码块文本行——fence 状态机（括号配对语义）。

    用途（审计 I2）: ``[[1, 2, 3]]`` 这类代码块内的列表字面量被
    wikilink 检测/修复误判——代码块不是链接语境，必须跳过。

    语义: 未闭合 fence 时尾部按"在代码里"处理（保守——宁可漏检测，
    不误伤代码）。fence 闭合与否由 check 层独立检测（未闭合会
    retry/拒绝落盘），本工具不修复闭合，只保证不碰代码块内容。

    Args:
        content: 页面内容。

    Yields:
        非代码块内的文本行。
    """
    for line, in_code in _iter_code_runs(content):
        if not in_code:
            yield line


def count_unclosed_fences(content: str) -> int:
    """统计未闭合的代码块数——括号配对语义。

    正常闭合的块深度回到 0；结束时深度 > 0 即未闭合。
    块内的 ```python 不算新开（内容），裸 ``` 在块外不算闭。

    Args:
        content: 页面内容。

    Returns:
        未闭合块数（0 表示全部闭合）。
    """
    depth = 0
    for line in content.split("\n"):
        stripped = line.lstrip()
        if not stripped.startswith("```"):
            continue
        if _FENCE_OPEN.match(stripped):
            if depth == 0:
                depth = 1
        elif depth > 0:
            depth = 0
    return depth


def _extract_body(content: str) -> str:
    """提取 frontmatter 之后的正文。

    Args:
        content: 页面内容。

    Returns:
        正文（strip 后）；无 frontmatter 或格式异常返回空串。
    """
    if not content.startswith("---"):
        return ""
    try:
        end = content.index("\n---\n", 3)
    except ValueError:
        return ""
    return content[end + 5:].strip()


def _body_without_title(content: str) -> str:
    """正文去掉标题行后的剩余内容——'只有标题'与'正文过短'共用。

    Args:
        content: 页面内容。

    Returns:
        去掉 # 开头行后的正文（strip 后）。
    """
    body = _extract_body(content)
    return "\n".join(
        line for line in body.split("\n")
        if not line.strip().startswith("#")
    ).strip()


# wikilink 语法共享正则——group(1)=slug, group(2)=显示文本（无 | 时 None）。
# 检测侧两个消费点共用（wikilink 说明文字检查 + 死链检测），避免正则漂移。
# normalize 的 _WIKILINK_RE 保留不动——fix 路径有自己的首字符约束。
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _check_analyze_json(
    content: str, *,
    candidates: list[str] | None = None,
    extra_refs: set[str] | None = None,
) -> tuple[bool, str]:
    """校验 analyze 两段式输出——自由文本 + JSON 尾巴的字段完整性。

    candidates: search 阶段的候选页面路径列表。
    提供时校验 relationships 的 from/to 归属——引用必须在
    {候选 slug ∪ "current-doc"} 内，否则是 LLM 幻觉（审计 C5:
    ``entities/current-doc`` 这类给固定标识乱加前缀的脏值）。
    空内容在此返回 False——空响应判定归 check（审计 C1: 调用点
    不再单独检测，retry 层统一处理重试 + check_ok 记录）。

    Args:
        content: LLM 原始输出。
        candidates: search 阶段的候选页面路径列表。
        extra_refs: 额外的合法引用（当前文档真实 slug）。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    if not content.strip():
        return False, "输出为空——请输出自由分析 + ```json {...}``` 尾巴。"
    # from/to 合法集合由候选归一化而来——校验逻辑的内部构造，调用方只给原始候选
    valid_refs: set[str] | None = None
    if candidates:
        valid_refs = {c.replace("wiki/", "").replace(".md", "") for c in candidates}
        valid_refs.add("current-doc")
        if extra_refs:
            valid_refs |= {
                r.replace("wiki/", "").replace(".md", "").strip()
                for r in extra_refs
            }

    _, json_part = _extract_analyze_parts(content)
    if not json_part:
        return False, "缺少结构化尾巴——请按格式输出: 自由分析 + ```json {...}```。"
    try:
        data = json.loads(json_part)
    except json.JSONDecodeError as e:
        return False, f"JSON 尾巴格式错误: {e}。请修正 ```json 块中的内容。"

    if not isinstance(data, dict):
        return False, "JSON 尾巴必须是对象 {}。"

    # ── entities ──
    entities = data.get("entities", [])
    if not isinstance(entities, list):
        return False, "entities 必须是数组。"
    for i, e in enumerate(entities):
        if not isinstance(e, dict):
            return False, f"entities[{i}] 必须是对象。"
        if "name" not in e or "type" not in e:
            return False, f"entities[{i}] 缺少 name 或 type 字段。"
        if not str(e.get("type", "")).strip():
            return False, f"entities[{i}].type 不能为空。"
        importance = e.get("importance", "")
        if importance and importance not in _VALID_IMPORTANCE:
            return False, f"entities[{i}].importance 必须是 核心/边缘，当前: {importance}。"

    # ── concepts ──
    concepts = data.get("concepts", [])
    if not isinstance(concepts, list):
        return False, "concepts 必须是数组。"
    for i, c in enumerate(concepts):
        if not isinstance(c, dict):
            return False, f"concepts[{i}] 必须是对象。"
        if "name" not in c:
            return False, f"concepts[{i}] 缺少 name 字段。"
        importance = c.get("importance", "")
        if importance and importance not in _VALID_IMPORTANCE:
            return False, f"concepts[{i}].importance 必须是 核心/边缘，当前: {importance}。"

    # ── relationships ──
    relationships = data.get("relationships", [])
    if not isinstance(relationships, list):
        return False, "relationships 必须是数组。"
    for i, r in enumerate(relationships):
        if not isinstance(r, dict):
            return False, f"relationships[{i}] 必须是对象。"
        if "from" not in r or "to" not in r:
            return False, f"relationships[{i}] 缺少 from 或 to 字段。"
        if "relation" not in r:
            return False, f"relationships[{i}] 缺少 relation 字段。"
        if r.get("relation") not in _VALID_RELATIONS:
            return False, f"relationships[{i}].relation 必须是 {_VALID_RELATIONS} 之一，当前: {r.get('relation')}。"
        # 归属校验——from/to 必须指向候选页面或 current-doc
        if valid_refs:
            for field in ("from", "to"):
                raw_v = str(r.get(field, "")).strip()
                norm_v = raw_v.replace("wiki/", "").replace(".md", "")
                if norm_v not in valid_refs:
                    return False, (
                        f"relationships[{i}].{field} 引用不存在: {raw_v!r}。"
                        f"合法值: \"current-doc\" 或候选页面 slug "
                        f"（{sorted(valid_refs)[:6]}...）"
                    )

    return True, ""

def _check_plan_json(
    content: str, *,
    allowed_dispositions: set[str] | None = None,
) -> tuple[bool, str]:
    """校验 plan 阶段的 JSON 输出——结构与字段级校验。

    与 analyze 的尾巴校验同风格: 逐字段检查，错误消息可执行，
    让 LLM 在 retry 时知道自己错在哪。只校验'说得对不对'，
    不校验'引用存不存在'——那由 _parse_plan 后的 filter_plan_refs 做。

    Args:
        content: LLM 原始输出。
        allowed_dispositions: 模式契约（prompt 模块的
            ALLOWED_DISPOSITIONS）。refine 只允许 update——
            LLM 输出 new 直接 retry 修正。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    allowed = allowed_dispositions or _VALID_DISPOSITIONS
    # fence/尾部缺括号是格式化噪声不是内容错误——与 _parse_plan 共享
    # 同一格式规约（_strip_fence 内含 I5 repair）。关掉 thinking 后
    # LLM 输出风格变化（爱包裹 ```json、深嵌套少写尾部 }），必须容忍。
    cleaned = _strip_fence(content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # 错误消息可执行化——原始报错 "line 1 column 891" 对 LLM
        # 不可行动，改成括号配对的行动指引
        return False, (
            f"JSON 格式错误: {e}。"
            f"请输出合法的 JSON——检查最外层与 page_targets/references "
            f"各层括号是否配对闭合，数组元素间用逗号分隔。"
        )
    if not isinstance(data, dict):
        return False, "根必须是 JSON 对象 {}，不是数组。"

    targets = data.get("page_targets", [])
    if not isinstance(targets, list):
        return False, "缺少 page_targets 数组字段。"

    seen_paths: set[str] = set()
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            return False, f"page_targets[{i}] 必须是对象。"
        if "wiki_path" not in t or "title" not in t or "disposition" not in t:
            return False, f"page_targets[{i}] 缺少 wiki_path/title/disposition 字段。"
        path = str(t.get("wiki_path", "")).strip()
        if not path:
            return False, f"page_targets[{i}].wiki_path 不能为空。"
        # 路由校验——页面只允许落在 schema 定义的内容目录。
        # （实测事故: LLM 造出 languages/python.md、tools/sphinx.md，
        # 四个内容目录外的页面在 scan_wiki 里完全隐形）
        first_seg = path.replace("wiki/", "").split("/", 1)[0]
        if first_seg not in ("concepts", "entities", "topics"):
            return False, (
                f"page_targets[{i}].wiki_path 目录非法: {path!r}。"
                f"只允许 concepts/ entities/ topics/ 三个内容目录"
                f"（sources/ 由系统维护，禁止生成）。"
            )
        if path in seen_paths:
            return False, f"page_targets[{i}].wiki_path 重复: {path}。同一页面只能出现一次。"
        seen_paths.add(path)

        disposition = t.get("disposition")
        if disposition not in allowed:
            return False, (
                f"page_targets[{i}].disposition 必须是 {sorted(allowed)} 之一，"
                f"当前: {disposition!r}。"
                f"不操作的页面不要写进 page_targets——输出空数组即可。"
            )
        title = str(t.get("title", "")).strip()
        if not title:
            return False, f"page_targets[{i}].title 不能为空。"
        reason = t.get("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            return False, (
                f"page_targets[{i}].reason 不能为空——写明具体操作: "
                f"从哪提取内容、补充到哪个章节、应包含哪些关键点。"
            )

        # references 必须是对象数组，slug 非空
        refs = t.get("references", [])
        if not isinstance(refs, list):
            return False, f"page_targets[{i}].references 必须是数组。"
        for j, r in enumerate(refs):
            if not isinstance(r, dict) or not str(r.get("slug", "")).strip():
                return False, (
                    f"page_targets[{i}].references[{j}] 格式错误——"
                    f"必须是 {{\"slug\": \"...\", \"reason\": \"...\"}} 且 slug 非空。"
                )

    return True, ""

def _check_json_array(content: str) -> tuple[bool, str]:
    """校验 JSON 字符串数组输出。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    # fence 容错——与 _check_plan_json 共享 _strip_fence
    cleaned = _strip_fence(content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return False, f"JSON 格式错误: {e}。请输出合法的 JSON 字符串数组。"
    if not isinstance(data, list):
        return False, "请输出 JSON 数组格式，如 [\"a\", \"b\"]。"
    for item in data:
        if not isinstance(item, str):
            return False, "数组中每个元素必须是字符串。"
    return True, ""


def _check_page_body(content: str) -> tuple[bool, str]:
    """正文存在性——frontmatter 后有正文、去掉标题后仍有内容。

    quality.check_page_quality 复用本判定组装 Issue（判定只有这一份）。

    Args:
        content: 页面内容。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    body = _extract_body(content)
    if not body:
        return False, "frontmatter 后无正文——请在 frontmatter 之后写出完整页面内容。"
    if not _body_without_title(content):
        return False, (
            "正文只有标题，无实际内容——请补充段落、代码示例或说明文字。"
        )
    return True, ""


def _check_page_output(content: str) -> tuple[bool, str]:
    """页面输出总闸门——frontmatter 必填 + 正文存在 + 格式，任一不过即重试。

    组合顺序: frontmatter 先查（结构性问题，错误消息更基础），
    正文存在次之，wikilink/fence 最后（正文格式）。三个 check
    独立保持可复用，组合只在这一处（页面生成的唯一 check 入口）。

    Args:
        content: LLM 生成的页面内容。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    ok, err = _check_page_frontmatter(content)
    if not ok:
        return ok, err
    ok, err = _check_page_body(content)
    if not ok:
        return ok, err
    return _check_wikilink_has_text(content)


def _check_page_frontmatter(
    content: str, *,
    required: tuple[str, ...] = ("type", "title", "summary", "goal"),
) -> tuple[bool, str]:
    """校验页面输出的 frontmatter 必填字段。

    Args:
        content: 页面内容。
        required: 必填字段集——goal 是页面使命锚，缺失时 LLM
            重试补写。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    fm, _ = split_frontmatter(content)
    if not fm:
        return False, "页面缺少 YAML frontmatter（--- 包裹的头部）。"
    missing = [k for k in required if not fm.get(k, "").strip()]
    if missing:
        return False, f"frontmatter 缺少必填字段: {', '.join(missing)}。"
    return True, ""


def _check_wikilink_has_text(content: str) -> tuple[bool, str]:
    """校验页面输出——wikilink 带说明文字 + 代码块闭合。

    跳过 frontmatter 区域（---...---）中的内容。

    Args:
        content: 页面内容。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    # 切掉 frontmatter——里面的 related/tags 不是 wikilink。
    # 无 frontmatter 时退化为全文（本 check 单独调用也要能工作）
    body = _extract_body(content) or content

    bare_links: list[str] = []
    # 只查非代码行（审计 I2）——代码块里的 [[1, 2, 3]] 不是链接
    text = "\n".join(iter_text_outside_code(body))
    for m in _WIKILINK_RE.finditer(text):
        # group(2) 是显示文本——None 说明没有 |，即裸链接
        if m.group(2) is None:
            bare_links.append(m.group(0))

    if bare_links:
        examples = ", ".join(bare_links[:3])
        return False, (
            f"以下 wikilink 缺少 | 说明文字: {examples}。"
            f"每个 [[wikilink]] 必须写成 [[slug|显示文本]] 格式。"
        )

    # 代码块闭合——括号配对语义（带标记 ```python = 左括号，裸 ``` = 右括号）。
    # 旧奇数计数假阴性: 两个 ```python（2 开 0 闭）被误判已闭合，标题被吞。
    unclosed = count_unclosed_fences(body)
    if unclosed:
        return False, (
            f"代码块未闭合（{unclosed} 个块缺闭合 ```）——"
            f"每个 ```python 开块都要有配对的裸 ``` 闭合。"
        )

    return True, ""
