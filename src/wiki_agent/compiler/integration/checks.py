"""集成层校验——LLM 阶段输出"什么算好"的 check 回调。

与 parse 的分界: 本模块校验（返回 (ok, reason)，错误消息可执行——
LLM 重试时知道错在哪），parse 解析。共享 _strip_fence（从 integration.parse）。

页面级闸门不在这里——`_check_page_output` 等在 wiki/rules.py（无 LLM 底层），
execute 与 wiki 体检复用的页面判定唯一来源。
"""

from __future__ import annotations

import json

from wiki_agent.compiler.integration.parse import _extract_analyze_parts, _strip_fence

_VALID_RELATIONS = {"duplicate", "extends", "related", "contradicts", "unrelated"}
# "重要" 是 LLM 的自然语言高频词（实测 refine 3 次违规全是它）——
# 枚举拦截性价比低，并入合法集
_VALID_IMPORTANCE = {"核心", "边缘", "重要"}
_VALID_DISPOSITIONS = {"new", "update"}
# 页面类型与目录的权威映射（与 quality._TYPE_DIRS 同义——plan 校验先行，
# 落盘闸门兜底，两处一致）
_VALID_PAGE_TYPES = {"concept", "entity", "topic"}
_TYPE_DIRS = {"concept": "concepts", "entity": "entities", "topic": "topics"}


def check_analyze_json(
    content: str,
    *,
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
            valid_refs |= {r.replace("wiki/", "").replace(".md", "").strip() for r in extra_refs}

    _, json_part = _extract_analyze_parts(content)
    if not json_part:
        return False, "缺少结构化尾巴——请按格式输出: 自由分析 + ```json {...}```。"
    try:
        data = json.loads(json_part)
    except json.JSONDecodeError as e:
        return False, f"JSON 尾巴格式错误: {e}。请修正 ```json 块中的内容。"

    if not isinstance(data, dict):
        return False, "JSON 尾巴必须是对象 {}。"

    # entities
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

    # concepts 
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

    # relationships
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
        # 归属校验——from/to 必须指向候选页面或 current-doc
        if valid_refs:
            for field in ("from", "to"):
                raw_v = str(r.get(field, "")).strip()
                norm_v = raw_v.replace("wiki/", "").replace(".md", "")
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
        # new 页面必须由 plan 决策 page_type，且与路由目录一致——
        # type 与目录来自同一次决策，generate 阶段不再自行判断
        # （实测: plan 路由 entities/、generate 写 type=concept，
        # 质量闸门 type/目录不一致，页面生成失败）。
        page_type = str(t.get("page_type", "")).strip()
        if disposition == "new":
            if page_type not in _VALID_PAGE_TYPES:
                return False, (
                    f"page_targets[{i}]（{path}）是 new，必须给出 page_type，"
                    f"且只能是 {sorted(_VALID_PAGE_TYPES)} 之一，当前: {page_type!r}。"
                )
            if _TYPE_DIRS.get(page_type) != first_seg:
                return False, (
                    f"page_targets[{i}]（{path}）page_type={page_type!r} 与目录不一致: "
                    f"type={page_type} 应位于 {_TYPE_DIRS[page_type]}/ 下。"
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

        # references 必须是对象数组，slug 非空
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


def check_json_array(content: str) -> tuple[bool, str]:
    """校验 JSON 字符串数组输出。

    Args:
        content: LLM 原始输出。

    Returns:
        (是否通过, 可执行的错误消息)。
    """
    # fence 容错——与 check_plan_json 共享 _strip_fence
    cleaned = _strip_fence(content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return False, f"JSON 格式错误: {e}。请输出合法的 JSON 字符串数组。"
    if not isinstance(data, list):
        return False, '请输出 JSON 数组格式，如 ["a", "b"]。'
    for item in data:
        if not isinstance(item, str):
            return False, "数组中每个元素必须是字符串。"
    return True, ""
