"""页面规范化——LLM 草稿与最终落盘之间的全部加工（写路径）。

主线: 修 LLM 输出 → 盖系统权威字段 → 质检闸门兜底。
**顺序是领域知识**——由编排入口一次完成，调用方不应自己组装。
质检与全库体检属审计路径（quality 模块），本模块只负责"把页面做成最终形态"。
"""

from __future__ import annotations

import re

from wiki_agent.log import get_logger
from wiki_agent.wiki.quality import Issue, check_page_quality
from wiki_agent.wiki.rules import _iter_code_runs, iter_text_outside_code

logger = get_logger("NORMALIZE")

_WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9][^\]|]+?)(?:\|([^\]]+?))?\]\]")


# fix —— 修 LLM 输出


def fix_markdown_fence(content: str) -> str:
    """去掉 LLM 输出中的格式噪声。

    处理 4 种常见情况:
    1. 开头 stray fence: ```markdown / ```yaml
    2. 结尾 stray fence: ```
    3. frontmatter 后 stray fence: ---\\n```
    4. LLM 在 frontmatter 前插入的说明文字

    Args:
        content: LLM 生成的原始内容。

    Returns:
        清理后的内容。
    """
    content = content.strip()
    # 4. 不以 --- / ``` / # 开头 → 切到第一个 frontmatter 或 fence
    if content and content[0] not in {"-", "`", "#"}:
        first_fm = content.find("\n---\n")
        first_fence = content.find("\n```")
        start = -1
        if first_fm >= 0:
            start = first_fm
        if first_fence >= 0 and (start < 0 or first_fence < start):
            start = first_fence + 1  # include the newline before ```
        if start > 0:
            content = content[start:].strip()

    # 1. 开头 fence
    content = re.sub(r"^```(?:markdown|md|yaml|text)?\s*\n?", "", content)
    # 2. 结尾 fence
    content = re.sub(r"\n?```\s*$", "", content)
    # 3. frontmatter 闭合后、正文前的 stray ``` 行——
    #    只处理 frontmatter 后紧跟的 stray fence（count=1 + 锚定 frontmatter）。
    #    事故教训（2026-08-15）: 旧实现全局替换 `\n```\n# 标题`，把
    #    合法代码块的裸闭合（块结束紧跟下一节标题）全删了——
    #    3 开 3 闭的完好页面被修成 3 开 0 闭，落盘后标题全被吞。
    if content.startswith("---"):
        try:
            fm_end = content.index("\n---\n", 3)
        except ValueError:
            fm_end = -1
        if fm_end >= 0:
            rest = content[fm_end + 5 :]
            m = re.match(r"\n?```\s*\n", rest)
            if m:
                content = content[: fm_end + 5] + rest[m.end() :]
    return content.strip()


def fix_wikilinks(content: str, *, valid_slugs: set[str]) -> str:
    """去掉正文中指向不存在页面的 [[wikilink]]——变为纯文本。

    跳过代码块（审计 I2）——代码里的 ``[[1, 2, 3]]`` 列表字面量
    不是链接，修复它们会破坏 Python 代码。

    Args:
        content: 页面内容。
        valid_slugs: 有效页面 slug 集合（如 {"entities/wraps", "concepts/lambda"}）。

    Returns:
        修复后的内容。
    """
    if not valid_slugs:
        return content

    def _replace(m: re.Match) -> str:
        full = m.group(0)
        slug = m.group(1).strip().replace(".md", "")
        text = m.group(2)
        if slug not in valid_slugs:
            plain = text.strip() if text else slug.rsplit("/", 1)[-1]
            logger.warning("  wikilink 无效 '%s' → 转为纯文本 '%s'", full, plain)
            return plain
        return full

    # 逐行处理: 只在非代码行替换（括号配对 fence 状态机）
    lines: list[str] = []
    for line, in_code in _iter_code_runs(content):
        lines.append(line if in_code else _WIKILINK_RE.sub(_replace, line))
    return "\n".join(lines)


# inject —— 系统权威字段


def extract_related(content: str, *, valid_slugs: set[str]) -> str:
    """从正文 wikilink 自动提取 related 字段——LLM 不写 related，代码生成。

    动机: YAML 数组对 LLM 是高翻车区（裸名/引号/括号混合错误），
    而正文 wikilink 它已经写得很顺（死链 0 条）。
    用代码从正文提取，格式 100% 一致。

    规则:
    - 扫描正文所有 [[wikilink]]（去 .md 后缀）→ 去重
    - 只保留 valid_slugs 内的（死链已被 fix_wikilinks 转纯文本）
    - 写入 frontmatter 的 related 行（覆盖 LLM 写的任何值）
    - 无链接 → related: []

    Args:
        content: 页面内容。
        valid_slugs: 有效页面 slug 集合。

    Returns:
        更新 related 后的内容。
    """
    if not content.startswith("---"):
        return content
    try:
        end = content.index("\n---\n", 3)
    except ValueError:
        return content
    frontmatter = content[:end]
    body = content[end:]

    # 从正文提取有效链接（跳过代码块——审计 I2 同款）
    slugs: list[str] = []
    seen: set[str] = set()
    body_text = "\n".join(iter_text_outside_code(body))
    for wm in _WIKILINK_RE.finditer(body_text):
        slug = wm.group(1).strip().replace(".md", "")
        if slug in valid_slugs and slug not in seen:
            seen.add(slug)
            slugs.append(slug)

    new_related = 'related: ["[[' + ']]", "[['.join(slugs) + ']]"]' if slugs else "related: []"
    if re.search(r"(?m)^\s*related\s*:", frontmatter):
        fixed_fm = re.sub(
            r"(?m)^\s*related\s*:.*$",
            new_related,
            frontmatter,
            count=1,
        )
    else:
        fixed_fm = frontmatter + f"\n{new_related}"

    if slugs:
        logger.info("  related 自动提取: %d 个链接", len(slugs))
    return fixed_fm + body


def inject_title_from_h1(content: str) -> str:
    """用正文首个非代码块 H1 覆盖 frontmatter 的 title。

    页面正文的 H1 是编辑器和用户实际看到的标题；LLM 可能分别生成
    ``page_target.title``、frontmatter title 和 H1，三者偶尔不一致。
    将 H1 作为最终规范标题，可以让索引、frontmatter 和正文保持一致。

    没有 H1 时不猜标题，保留原内容并交给页面质量闸门处理。

    Args:
        content: 页面完整内容。

    Returns:
        title 与首个 H1 一致的页面内容。
    """
    if not content.startswith("---"):
        return content
    try:
        end = content.index("\n---\n", 3)
    except ValueError:
        return content

    body = content[end + 5 :]
    title = ""
    for line in iter_text_outside_code(body):
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match and match.group(1).strip():
            title = match.group(1).strip().rstrip("#").strip()
            break
    if not title:
        return content

    frontmatter = content[:end]
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    replacement = f'title: "{escaped}"'
    if re.search(r"(?m)^\s*title\s*:", frontmatter):
        frontmatter = re.sub(
            r"(?m)^\s*title\s*:.*$",
            replacement,
            frontmatter,
            count=1,
        )
    else:
        frontmatter += f"\n{replacement}"
    return frontmatter + content[end:]


def inject_metadata(
    content: str,
    *,
    source_identity: str,
    today: str,
    existing: dict | None,
    page_type: str = "",
) -> str:
    """注入 created/updated/sources/type 到 frontmatter——系统权威字段，无视 LLM。

    - created: 新页面用 today，已有页面保留旧的
    - updated: 始终改为 today
    - sources: 追加 source_identity（去重）
    - type: page_type 非空时强制覆盖——路由与类型是 plan 的单一权威决策

    防御: LLM 偶发输出空 frontmatter（``---\\n---``）或连续 frontmatter，
    先合并再注入——否则第二段会被误当正文，产生双重 frontmatter。

    Args:
        content: 页面内容。
        source_identity: 源文档标识（追加进 sources）。
        today: 当天日期（updated 用）。
        existing: 已有页面 frontmatter（保留旧 created/sources）。
        page_type: plan 决策的页面类型（new 页面）；空串不改动。

    Returns:
        注入元数据后的内容。
    """
    if not content.startswith("---"):
        return content

    # 合并连续/空 frontmatter: "---\n---\ntype: x" → "---\ntype: x"
    content = re.sub(r"^---\s*\n+---\s*\n", "---\n", content, count=1)

    try:
        end = content.index("\n---\n", 3)
    except ValueError:
        return content

    fm = content[len("---\n") : end]
    rest = content[end:]

    existing_sources = existing or {}
    old_created = existing_sources.get("created", today)
    old_sources = existing_sources.get("sources", "")

    new_fm = fm
    # created: 保留旧的（追加路径同样用 old_created——LLM 不写 created，
    # 追加才是常规路径，用 today 会把已有页面的 created 重置）
    new_fm = re.sub(r"^created:.*$", f"created: {old_created}", new_fm, flags=re.MULTILINE)
    if "created:" not in fm:
        new_fm += f"\ncreated: {old_created}"
    # updated: 覆盖
    new_fm = re.sub(r"^updated:.*$", f"updated: {today}", new_fm, flags=re.MULTILINE)
    if "updated:" not in fm:
        new_fm += f"\nupdated: {today}"
    # sources: 合并去重
    sources = set(old_sources.strip("[]").replace('"', "").split(","))
    sources.add(source_identity)
    sources.discard("")
    sources_str = ", ".join(f'"{s.strip()}"' for s in sources if s.strip())
    if "sources:" in new_fm:
        new_fm = re.sub(
            r"^sources:.*$",
            f"sources: [{sources_str}]",
            new_fm,
            flags=re.MULTILINE,
        )
    else:
        new_fm += f"\nsources: [{sources_str}]"
    # type: plan 决策的单一权威——generate 写的 type 只是占位，
    # 以系统注入为准（空串=update 目标，沿用已有页面 type 不动）
    if page_type:
        if "type:" in new_fm:
            new_fm = re.sub(r"^type:.*$", f"type: {page_type}", new_fm, flags=re.MULTILINE)
        else:
            new_fm += f"\ntype: {page_type}"

    return f"---\n{new_fm}{rest}"


# 规范化编排——页面生成主路径的唯一入口


def normalize_page(
    content: str,
    *,
    path: str,
    valid_slugs: set[str],
    source_identity: str = "",
    today: str = "",
    existing: dict | None = None,
    page_type: str = "",
) -> tuple[str, list[Issue]]:
    """页面规范化——完整处理链的唯一入口。

    fix（修 LLM 脏）→ inject（系统权威）→ check（最后闸门）:
    1. fix_markdown_fence → fix_wikilinks
    2. inject_metadata（含 plan 决策的 page_type 覆盖）→ extract_related
    3. check_page_quality（quality 模块）

    Args:
        content: LLM 生成的页面内容。
        path: 页面相对路径（质检定位）。
        valid_slugs: 有效页面 slug 集合。
        source_identity: 源文档标识。
        today: 当天日期。
        existing: 已有页面 frontmatter。
        page_type: plan 决策的页面类型（new 页面）。非空时强制覆盖
            frontmatter 的 type——路由与类型是 plan 的单一权威决策，
            generate 的 type 只是占位，以系统注入为准。

    Returns:
        (规范化后的 content, issues)。issues 含 error 时调用方拒绝落盘。
    """
    # 1. 修复 LLM 输出
    content = fix_markdown_fence(content)
    content = fix_wikilinks(content, valid_slugs=valid_slugs)
    content = inject_title_from_h1(content)

    # 2. 系统权威字段
    content = inject_metadata(
        content,
        source_identity=source_identity,
        today=today,
        existing=existing,
        page_type=page_type,
    )
    content = extract_related(content, valid_slugs=valid_slugs)

    # 3. 质量检测
    issues = check_page_quality(content, path=path)
    return content, issues
