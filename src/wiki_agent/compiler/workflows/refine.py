"""refine 的纯函数侧——页面清单与 index 视图。

refine 是 wiki 自编译：以 wiki 页面自身为输入重跑编译链，刷新关系。
与 compile/sync 的关键差异:
- 输入是 wiki 内容页（页面自己更新自己）
- index 视图排除当前页条目——否则检索必然选中自己 → 判重 →
  用页面摘要重写页面（信息损耗）
- 不存 source 档案页（输入就是 wiki 页面，再存 = 自我复制）

时机: 全文 index 建立后（增量生成时目标页面还不存在，链接先天不充分）。
执行/排队在 application.wiki_ops 与 jobs.service，这里不装配。
"""

from __future__ import annotations

from pathlib import Path

# refine 输入范围——知识页三目录（sources/ 是源档案页不参与；index 等系统文件排除）
CONTENT_DIRS = ("concepts", "entities", "topics")


def refine_pages(wiki_dir: str | Path) -> list[Path]:
    """收集 refine 输入——wiki 下三目录的全部 .md 页面。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        排序后的页面路径列表。
    """
    wiki = Path(wiki_dir)
    pages: list[Path] = []
    for sub in CONTENT_DIRS:
        d = wiki / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def build_index_excluding_self(wiki_dir: str | Path):
    """构造 index_reader 钩子——返回排除指定源页面条目的 index 文本。

    排除方式: 去掉含 ``[[slug]]`` 的条目行。slug = 源路径相对 wiki 去 .md。
    条目行格式: ``- [[concepts/x]] — [concept] concepts/x.md — 标题``。

    Args:
        wiki_dir: wiki 根目录。

    Returns:
        reader 钩子（接收源路径，返回排除自身后的 index 文本）。
    """
    wiki = Path(wiki_dir)

    def reader(source_path: Path) -> str:
        index_path = wiki / "index.md"
        try:
            content = index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        rel = Path(source_path).relative_to(wiki)
        slug = str(rel).replace(".md", "")
        # 逐行过滤含自身 slug 的条目——保留空行结构
        kept = [line for line in content.split("\n") if f"[[{slug}]]" not in line]
        return "\n".join(kept)

    return reader
