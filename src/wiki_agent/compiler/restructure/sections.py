"""页面章节切分——分配路由与装配的输入形状。

章节 = "## " 级标题及其正文；H1 与导语归入页首（heading ""）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from wiki_agent.wiki.frontmatter import split_frontmatter


def _prose_paragraphs(body: str) -> list[str]:
    """按空行切段；标题、引用、围栏行与代码块内部不算散文。

    代码块开头的章节此前会把 "```python" 当首段摘要——路由看到的是
    零信息行。围栏及其内部整段跳过。
    """
    paras: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        if in_fence or not s or s.startswith(("#", ">", "!")):
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        cur.append(s)
    if cur:
        paras.append(" ".join(cur))
    return paras


@dataclass(frozen=True, slots=True)
class Section:
    slug: str
    heading: str  # "" = 页首（H1+导语）
    body: str

    @property
    def id(self) -> str:
        # 页首不用空标题结尾的 id：真实模型会把 "slug::" 连同大纲行的
        # 说明列一起抄进分配表，边界必须显式。
        return f"{self.slug}::{self.heading}" if self.heading else f"{self.slug}::§top"

    def gist(self, limit: int) -> str:
        """大纲行用的摘要：前两段开头、共享 limit 预算。

        页首节的导语（H1 后到第一个 ## 的全部段落）同样按段取——
        导语是 H1 下最有判断价值的文字，此前只取到第一行 80 字符。
        """
        paras = _prose_paragraphs(self.body)
        if not paras:
            return ""
        if len(paras) == 1:
            return paras[0][:limit]
        half = limit // 2
        out = paras[0][:half]
        if limit - half - 1 > 0:
            out += " / " + paras[1][: limit - half - 1]
        return out


def page_sections(md_path: Path, slug: str) -> list[Section]:
    _, body = split_frontmatter(md_path.read_text(encoding="utf-8"))
    parts = re.split(r"(?m)^## (.+?)\s*$", body)
    sections: list[Section] = []
    head = parts[0].strip()
    if head:
        sections.append(Section(slug=slug, heading="", body=head))
    for heading, chunk in zip(parts[1::2], parts[2::2], strict=True):
        sections.append(Section(slug=slug, heading=heading.strip(), body=chunk.strip()))
    return sections
