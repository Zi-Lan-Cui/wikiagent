"""页面章节切分——分配路由与装配的输入形状。

章节 = "## " 级标题及其正文；H1 与导语归入页首（heading ""）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from wiki_agent.wiki.frontmatter import split_frontmatter


@dataclass(frozen=True, slots=True)
class Section:
    slug: str
    heading: str  # "" = 页首（H1+导语）
    body: str

    @property
    def id(self) -> str:
        return f"{self.slug}::{self.heading}"

    @property
    def gist(self) -> str:
        """大纲行用的摘要：标题后首段前 80 字符。"""
        for line in self.body.splitlines():
            text = line.strip()
            if text and not text.startswith(("#", "!", ">")):
                return text[:80]
        return ""


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
