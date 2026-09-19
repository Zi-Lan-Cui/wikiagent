"""Wiki 页面检测原子与闸门——"什么算好页面"（无 LLM，底层）。

原 checks.py 的 wiki 侧。分层（依赖单向）:
    frontmatter.py  frontmatter 解析（被本模块复用）
    rules.py        本模块——fence 状态机/正文提取原子 + 页面 check 回调
    quality.py      体检（Issue 组装 + scan_wiki + 报告，判定复用本模块）
    normalize.py    修内容（fix/inject）+ 定稿兜底

页面闸门判定只在本模块一份定义——integration 生成时的执行闸门与 wiki
落盘后的 scan 体检复用同一 `_check_page_output`，同一现象两种语境、一份判定。
"""

from __future__ import annotations

import re

from wiki_agent.wiki.frontmatter import split_frontmatter

# 共享检测原子——quality 的 scan 与 normalize 的修复都建在这些原子上

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
    return content[end + 5 :].strip()


def _body_without_title(content: str) -> str:
    """正文去掉标题行后的剩余内容——'只有标题'与'正文过短'共用。

    Args:
        content: 页面内容。

    Returns:
        去掉 # 开头行后的正文（strip 后）。
    """
    body = _extract_body(content)
    return "\n".join(line for line in body.split("\n") if not line.strip().startswith("#")).strip()


# wikilink 语法共享正则——group(1)=slug, group(2)=显示文本（无 | 时 None）。
# 检测侧两个消费点共用（wikilink 说明文字检查 + 死链检测），避免正则漂移。
# normalize 的 _WIKILINK_RE 保留不动——fix 路径有自己的首字符约束。
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


# 页面 check 回调——返回 (ok, reason)，错误消息可执行


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
        return False, ("正文只有标题，无实际内容——请补充段落、代码示例或说明文字。")
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
    content: str,
    *,
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
