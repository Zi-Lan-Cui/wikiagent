"""内容页面目录模型——目录/类型映射与页面身份原语的唯一来源（无 LLM，底层）。

wiki 层的检查、compile 的路由校验、维护的名册与装配共用这一份定义；
新增内容目录只改 PAGE_TYPE_BY_DIR。slug/path 换算与 index 行格式也归
这里——此前各处手写 `.replace("wiki/", "")` 口径漂移（嵌套页丢中间
目录、`.md` 段被错切），页面存在性判定可能给出相反答案。
"""

from __future__ import annotations

from pathlib import Path

PAGE_TYPE_BY_DIR = {"concepts": "concept", "entities": "entity", "topics": "topic"}

CONTENT_DIRS = tuple(PAGE_TYPE_BY_DIR)

TYPE_DIR = {t: d for d, t in PAGE_TYPE_BY_DIR.items()}


def path_for(wiki_dir: str | Path, slug: str) -> Path:
    """slug → 页面文件路径。"""
    return Path(wiki_dir) / f"{slug}.md"


def slug_from_path(wiki_dir: str | Path, path: str | Path) -> str:
    """页面文件路径（wiki 根下）→ slug；保留中间目录层级。"""
    rel = Path(path).resolve().relative_to(Path(wiki_dir).resolve())
    return str(rel.with_suffix(""))


def slug_from_ref(ref: str) -> str:
    """页面引用（"wiki/concepts/a.md"、"/a.md"、"concepts/a"）→ slug。

    只剥首尾固定前后缀（removesuffix），不做全局 replace——路径中间
    出现 ".md/" 段时不会被错切。
    """
    cleaned = ref.strip()
    for prefix in ("wiki/", "/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    return cleaned.removesuffix(".md")


def index_line(slug: str, page_type: str, title: str, summary: str = "", goal: str = "") -> str:
    """index.md 行格式的唯一构造器。

    行携带 title/summary/goal——粗提与检索的一等信息源，任何写入口
    不得少列（goal 缺失即页面边界信息丢失）。
    """
    line = f"- [[{slug}]] — [{page_type}] {slug}.md — {title}"
    if summary:
        line += f" — {summary}"
    if goal:
        line += f" — 使命: {goal}"
    return line
