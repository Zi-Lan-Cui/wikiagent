"""frontmatter 解析——Wiki 页面头部的唯一实现（无 LLM，底层）。

供编译、维护和导航共用。
"""

from __future__ import annotations

from pathlib import Path


def split_frontmatter(content: str) -> tuple[dict, str]:
    """切分 frontmatter——返回 (字段 dict, 正文)。

    正文不 strip：调用方各自决定尾部处理
    （restructure 拼接要保留原文形态）。

    简单解析语义: 逐行 partition(": ")——不做完整 YAML
    （嵌套/列表/引号转义超出 wiki 页面的 frontmatter 需求）。

    Args:
        content: 完整页面内容。

    Returns:
        (字段 dict, 正文)。正文不 strip——调用方各自决定
        尾部处理（restructure 拼接要保留原文形态）。
    """
    fm: dict = {}
    body = content
    if content.startswith("---"):
        try:
            end = content.index("\n---\n", 3)
            for line in content[len("---\n") : end].split("\n"):
                if ": " in line:
                    k, _, v = line.partition(": ")
                    fm[k.strip()] = v.strip().strip("\"'")
            body = content[end + 5 :]
        except ValueError:
            pass
    return fm, body


def parse_frontmatter(path) -> dict:
    """读文件 + 解析 frontmatter——返回字段 dict。

    读失败返回空 dict（页面不可读 = 无元数据，不抛异常阻塞流水线）。

    Args:
        path: 文件路径（内部读文件）。

    Returns:
        frontmatter 字段 dict。
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    fm, _ = split_frontmatter(content)
    return fm
