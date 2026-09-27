"""frontmatter 解析——Wiki 页面头部的唯一实现（无 LLM，底层）。

供编译、维护和导航共用。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path


def split_frontmatter(content: str) -> tuple[dict, str]:
    """切分 frontmatter——返回 (字段 dict, 正文)。

    正文不 strip：调用方各自决定尾部处理
    （拼接场景要保留原文形态）。

    简单解析语义: 逐行 partition(": ")——不做完整 YAML
    （嵌套/列表/引号转义超出 wiki 页面的 frontmatter 需求）。

    Args:
        content: 完整页面内容。

    Returns:
        (字段 dict, 正文)。正文不 strip——调用方各自决定
        尾部处理（拼接场景要保留原文形态）。
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


def _render_value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in value) + "]"
    s = str(value)
    if s == "" or s[0] in "[{&*!|>%@`" or ": " in s or " #" in s or '"' in s:
        return json.dumps(s, ensure_ascii=False)
    return s


def render_frontmatter(fields: dict[str, object]) -> str:
    """从零合成一段 frontmatter（含首尾 --- 与末换行）。"""
    lines = ["---"] + [f"{k}: {_render_value(v)}" for k, v in fields.items()] + ["---", ""]
    return "\n".join(lines)


def set_fields(content: str, updates: Mapping[str, object]) -> str:
    """frontmatter 写回——split_frontmatter 的唯一对应写侧。

    已有 key 原位替换（保持行序），缺失 key 追加在 frontmatter 末尾；
    值支持 str 与 list[str]（渲染为行内列表）。content 没有完整
    frontmatter 时原样返回——是否合成由调用方决定。
    各处需要改 frontmatter 的都走这里，不再各自做行手术。
    """
    if not content.startswith("---"):
        return content
    lines = content.splitlines(keepends=True)
    dash = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    if len(dash) < 2:
        return content
    start, end = dash[0] + 1, dash[1]
    remaining = dict(updates)
    out: list[str] = []
    for i, ln in enumerate(lines):
        if start <= i < end:
            key = ln.partition(":")[0].strip()
            if key in remaining:
                out.append(f"{key}: {_render_value(remaining.pop(key))}\n")
                continue
        out.append(ln)
    insert_at = next(i for i, ln in enumerate(out) if ln.strip() == "---" and i > 0)
    for k, v in remaining.items():
        out.insert(insert_at, f"{k}: {_render_value(v)}\n")
        insert_at += 1
    return "".join(out)


def list_field(raw: object) -> list[str]:
    """把 frontmatter 列表字段归一化为字符串列表。

    简单解析对 sources/related 返回的是 '["a", "b"]' 形态的单字符串，
    也可能已是 list——两种存形都要能干净展开。
    """
    if not raw:
        return []
    if isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = str(raw).strip().strip("[]").split(",")
    return [s.strip().strip("\"'") for s in items if s.strip().strip("\"'")]


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
