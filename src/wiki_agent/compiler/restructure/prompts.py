"""维护流水线的 LLM 提示词：粗提、复核、路由、成文。

契约都收在 JSON 输出上，校验函数与各 prompt 同处一文件；解析与重试由
调用方（async_invoke_with_retry）负责。
"""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.rules import count_unclosed_fences
from wiki_agent.wiki.sections import TOP_LABEL, Section, pick_gist_limit

from .models import Unit


def outline(slugs: list[str], meta: dict[str, dict]) -> str:
    lines = []
    for slug in slugs:
        m = meta.get(slug, {})
        lines.append(f"- {slug} | {m.get('title', '')} | goal: {m.get('goal', '')} | {m.get('summary', '')}")
    return "\n".join(lines)


PROPOSE_SYSTEM = (
    "你是 wiki 结构维护的提议者。给定全部页面的目录大纲，找出结构问题并给出重组单元："
    "每个单元声明输入页（in，被整体消费）与输出页（out，slug+intent 说明这页将来讲什么）。"
    "合并=N→1，拆分/新建=1→M，改写=A→A（intent 给出改写方向），删除=out 空。"
    "不引入新知识：一切内容必须来自 in 页。合并与拆分的取舍以组织规范为准："
    "同一事物的不同侧面不是冗余，不同页面类型（concept/entity/topic）各司其职。"
    "没有值得动的结构就输出空数组。"
    '只输出 JSON：{"units":[{"in_pages":[...],"out":[{"slug":"concepts/x","intent":"..."}],"reason":"..."}]}'
)


def propose_user(index_content: str, outline: str, schema: str = "", purpose: str = "") -> str:
    parts = []
    if purpose:
        parts.append(f"## 知识库使命\n{purpose}")
    if schema:
        parts.append(f"## 组织规范\n{schema}")
    parts += [f"## 目录\n{index_content}", f"## 页面大纲\n{outline}", "给出重组单元（可为空）。"]
    return "\n\n".join(parts)


def check_propose_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"不是合法 JSON: {exc}"
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, list):
        return False, '缺少 units 数组。输出 {{"units": [...]}}。'
    for i, raw in enumerate(units):
        if not isinstance(raw, dict) or not isinstance(raw.get("in_pages"), list) or not raw["in_pages"]:
            return False, f"units[{i}] 需要非空 in_pages。"
        if not isinstance(raw.get("out"), list):
            return False, f"units[{i}] 缺少 out 数组（删除用空数组）。"
    return True, ""


RECHECK_SYSTEM = (
    "你是结构决定的人工复核代理。给定一个重组单元与涉及页的大纲，判断这个重组现在是否成立、"
    "方向是否正确（该不该合并/拆成这样）。结构决定影响面大，宁可放弃不可含糊。"
    '只输出 JSON：{{"keep": true|false, "reason": "..."}}'
)


def recheck_user(unit: Unit, outline: str, schema: str = "") -> str:
    intents = "；".join(f"{p.slug}: {p.intent or '（搬运）'}" for p in unit.out) or "（整页删除）"
    parts = [
        f"## 单元\n消费 {unit.in_pages} → 产出 [{intents}]\n理由: {unit.reason}",
        f"## 涉及页大纲\n{outline}",
    ]
    if schema:
        parts.insert(0, f"## 组织规范\n{schema}")
    parts.append("keep 还是放弃？")
    return "\n\n".join(parts)


def check_recheck_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"不是合法 JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("keep"), bool):
        return False, '需要 {{"keep": true/false, "reason": "..."}}。'
    return True, ""


ROUTE_SYSTEM = (
    "你是章节分配器。给定一组输入页的章节大纲与输出页的 intent，把每个章节分配到恰好一个输出页"
    "（删除单元则全部标 dropped——但只有输出为空时才允许，且必须来自显式删除提议）。"
    "禁止丢弃章节、禁止一稿多投、禁止发明确实不存在的目标。"
    '只输出 JSON：{{"assign": [{{"section": "章节id", "to": "out_slug"}}]}}。'
    "section 必须逐字复制大纲行开头的章节 id（含 :: 与 §top），"
    "不要包含竖线之后的标题或摘要文字。"
    "to 必须是输出页列表中给出的完整 slug（含目录前缀如 concepts/），"
    "不得回写输入页自身的 slug——输入页不是输出页时，它的章节只能进输出页。"
)


# 路由大纲的摘要预算：常规档每节两段开头共 160 字符；节数超过 20 的
# 大单元退化为短档（首句 40），控制提示规模与注意力稀释。
ROUTE_GIST_CHARS = 160
ROUTE_GIST_SHORT_CHARS = 40
ROUTE_OUTLINE_MAX_GIST_SECTIONS = 20


def route_outline(sections: list[Section]) -> str:
    limit = pick_gist_limit(
        len(sections), ROUTE_GIST_CHARS, ROUTE_GIST_SHORT_CHARS, ROUTE_OUTLINE_MAX_GIST_SECTIONS
    )
    return "\n".join(f"- {s.id} | {s.heading or TOP_LABEL} | {s.gist(limit)}" for s in sections)


def route_user(unit: Unit, sections: list[Section], fixed_note: str) -> str:
    intents = "；".join(f"{p.slug}: {p.intent}" for p in unit.out if not p.take)
    return (
        f"## 输出页 intent\n{intents or '（无待分配页）'}\n\n"
        f"## 章节大纲\n{route_outline(sections)}\n\n{fixed_note}"
    )


def check_route_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"不是合法 JSON: {exc}"
    assign = data.get("assign") if isinstance(data, dict) else None
    if not isinstance(assign, list):
        return False, '缺少 assign 数组。输出 {{"assign": [...]}}。'
    for i, item in enumerate(assign):
        if not isinstance(item, dict) or not item.get("section") or not item.get("to"):
            return False, f"assign[{i}] 需要 section 与 to。"
    return True, ""


REWRITE_SYSTEM = (
    "你是 wiki 页面成文者。给定装配好的草稿（章节按分配搬运而来）与该页的 intent 与旧版正文，"
    "重写成连贯页面：只使用给定材料，不新增事实，保留全部 [[链接]] 与图片引用，"
    "输出含 frontmatter 的完整 markdown。frontmatter 必须完整给出 type、title、"
    "summary、goal 四个字段（沿用旧版，新页从草稿的 frontmatter 继承，不得省略）。"
    "链接一律写成 [[slug|显示文字]]，禁止裸 [[slug]]；每个 ```python 代码块必须以裸 ``` 闭合。"
    "没有要改的就原样返回草稿。"
)


def check_rewrite_page(content: str) -> tuple[bool, str]:
    """成文输出形状校验——进重试层，把字段丢失与 fence 不闭合在请求内修一次。

    质量规则（wiki.rules）要求 frontmatter 必填字段与代码块闭合；此处用
    同口径预检，避免整单元因模型一次手滑就失败。
    """
    body = content.strip()
    if not body.startswith("---"):
        return False, "缺少 frontmatter——必须以 --- 开头。"
    try:
        fm, _ = split_frontmatter(body)
    except Exception:
        return False, "frontmatter 无法解析——检查 --- 成对。"
    missing = [k for k in ("type", "title", "summary", "goal") if not str(fm.get(k) or "").strip()]
    if missing:
        return False, f"frontmatter 缺少字段 {missing}——四个必填字段都要给出。"
    if count_unclosed_fences(body):
        return False, "代码块未闭合——每个 ```python 开块都要有配对的裸 ```。"
    return True, ""


def rewrite_user(
    slug: str, intent: str, draft: str, old: str, siblings: list[str]
) -> str:
    parts = [f"## 目标页\n{slug}\nintent: {intent or '（按草稿装配成文）'}"]
    if siblings:
        parts.append("## 同批产出页\n" + "、".join(siblings))
    if old:
        parts.append(f"## 旧版（结构与 frontmatter 基线）\n{old[:12000]}")
    parts.append(f"## 草稿（分配后的内容全集）\n{draft[:30000]}")
    parts.append("输出重写后的完整页面。")
    return "\n\n".join(parts)


def json_of(content: str) -> dict[str, Any]:
    data = json.loads(content)
    assert isinstance(data, dict)
    return data
