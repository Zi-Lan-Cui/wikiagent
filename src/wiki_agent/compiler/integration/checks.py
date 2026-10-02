"""集成层校验：LLM 阶段输出的 check 回调，返回 (ok, reason)。

错误消息会进入重试上下文，供模型定位并修正。与 parse 的分工：
本模块校验，parse 解析。
"""

from __future__ import annotations

import json

from wiki_agent.compiler.integration.parse import extract_analyze_parts, strip_fence
from wiki_agent.wiki.pages import CONTENT_DIRS, TYPE_DIR, slug_from_ref

_VALID_RELATIONS = {"duplicate", "extends", "related", "contradicts", "unrelated"}
# "重要"是模型输出核心程度时的常用同义词，直接并入合法集
_VALID_IMPORTANCE = {"核心", "边缘", "重要"}
VALID_DISPOSITIONS = {"new", "update"}
_VALID_PAGE_TYPES = set(TYPE_DIR)


def check_analyze_json(
    content: str,
    *,
    candidates: list[str] | None = None,
    extra_refs: set[str] | None = None,
) -> tuple[bool, str]:
    """校验 analyze 两段式输出：自由文本 + JSON 字段的完整性。

    提供 candidates 时校验 relationships 的 from/to，引用必须在
    {候选 slug ∪ "current-doc" ∪ extra_refs} 内，否则视为无效引用。
    空内容在此返回 False，由 retry 层统一处理重试。

    Args:
        content: LLM 原始输出。
        candidates: search 阶段的候选页面路径列表。
        extra_refs: 额外的合法引用（当前文档真实 slug）。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    if not content.strip():
        return False, "输出为空——请输出自由分析 + ```json {...}``` 尾巴。"
    # from/to 合法集：校验逻辑的内部构造，由调用方给的候选归一化而来
    valid_refs: set[str] | None = None
    if candidates:
        valid_refs = {slug_from_ref(c) for c in candidates}
        valid_refs.add("current-doc")
        if extra_refs:
            valid_refs |= {slug_from_ref(r) for r in extra_refs}

    _, json_part = extract_analyze_parts(content)
    if not json_part:
        return False, "缺少结构化尾巴——请按格式输出: 自由分析 + ```json {...}```。"
    try:
        data = json.loads(json_part)
    except json.JSONDecodeError as e:
        return False, f"JSON 尾巴格式错误: {e}。请修正 ```json 块中的内容。"

    if not isinstance(data, dict):
        return False, "JSON 尾巴必须是对象 {}。"

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
            return (
                False,
                f"relationships[{i}].relation 必须是 {_VALID_RELATIONS} 之一，当前: {r.get('relation')}。",
            )
        if valid_refs:
            for field in ("from", "to"):
                raw_v = str(r.get(field, "")).strip()
                norm_v = slug_from_ref(raw_v)
                if norm_v not in valid_refs:
                    return False, (
                        f"relationships[{i}].{field} 引用不存在: {raw_v!r}。"
                        f'合法值: "current-doc" 或候选页面 slug '
                        f"（{sorted(valid_refs)[:6]}...）"
                    )

    return True, ""


def check_plan_json(
    content: str,
    *,
    allowed_dispositions: set[str] | None = None,
) -> tuple[bool, str]:
    """校验 plan 阶段的 JSON 输出：结构与字段级校验。

    只校验格式与字段，不校验引用是否存在——那由 parse_plan 后的
    filter_plan_refs 做。

    Args:
        content: LLM 原始输出。
        allowed_dispositions: 各模式的合法处置集（prompt 模块的
            ALLOWED_DISPOSITIONS），越界输出经 retry 修正。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    allowed = allowed_dispositions or VALID_DISPOSITIONS
    # fence 包裹、尾部缺括号是格式噪声不是内容错误，与 parse_plan 同经
    # strip_fence 归一；关闭 thinking 后模型输出这类噪声更多，须容忍
    cleaned = strip_fence(content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # 原始报错只有位置信息，模型无从修正，改写为括号配对的指引
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
        # 页面只允许落在内容目录：目录外的页面 scan_wiki 扫描不到
        first_seg = slug_from_ref(path).split("/", 1)[0]
        if first_seg not in CONTENT_DIRS:
            return False, (
                f"page_targets[{i}].wiki_path 目录非法: {path!r}。"
                f"只允许 concepts/ entities/ topics/ 三个内容目录"
                f"（sources/ 不属于 Wiki 内容目录，禁止生成）。"
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
        # new 页面必须给出 page_type 且与路由目录一致：type 与目录出自
        # plan 同一次决策，generate 阶段不再判断，避免两阶段不一致
        page_type = str(t.get("page_type", "")).strip()
        if disposition == "new":
            if page_type not in _VALID_PAGE_TYPES:
                return False, (
                    f"page_targets[{i}]（{path}）是 new，必须给出 page_type，"
                    f"且只能是 {sorted(_VALID_PAGE_TYPES)} 之一，当前: {page_type!r}。"
                )
            if TYPE_DIR.get(page_type) != first_seg:
                return False, (
                    f"page_targets[{i}]（{path}）page_type={page_type!r} 与目录不一致: "
                    f"type={page_type} 应位于 {TYPE_DIR[page_type]}/ 下。"
                    f"请统一两者——改 wiki_path 或改 page_type。"
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

        refs = t.get("references", [])
        if not isinstance(refs, list):
            return False, f"page_targets[{i}].references 必须是数组。"
        for j, r in enumerate(refs):
            if not isinstance(r, dict) or not str(r.get("slug", "")).strip():
                return False, (
                    f"page_targets[{i}].references[{j}] 格式错误——"
                    f'必须是 {{"slug": "...", "reason": "..."}} 且 slug 非空。'
                )

    return True, ""


def check_paths_json(content: str) -> tuple[bool, str]:
    """校验 search 阶段输出：{"paths": ["entities/x.md", ...]}。

    对象与顶层数组两种格式都接受：调用已开 json_object，但兼容端点
    可能静默忽略 response_format，顶层数组是旧契约。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    cleaned = strip_fence(content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return False, (
            f'JSON 格式错误: {e}。请输出 {{"paths": ["entities/x.md", ...]}} 形式的 JSON 对象。'
        )
    if isinstance(data, dict):
        data = data.get("paths")
    if not isinstance(data, list):
        return False, 'paths 必须是字符串数组——{"paths": ["entities/x.md"]}，无结果时输出空数组。'
    for item in data:
        if not isinstance(item, str):
            return False, "paths 中每个元素必须是字符串路径。"
    return True, ""
