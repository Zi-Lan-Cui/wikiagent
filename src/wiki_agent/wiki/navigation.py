"""CLI/Web Wiki 导航的安全页面解析与检索。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wiki_agent.wiki.paths import safe_resolve


class WikiPageNotFound(FileNotFoundError):
    """页面不存在或超出公开 Wiki 范围。"""


@dataclass(frozen=True, slots=True)
class WikiPage:
    path: str
    content: str
    size: int
    updated_at: str
    metadata: dict[str, object]


def _frontmatter(content: str) -> dict[str, object]:
    """解析生成页面使用的精简 YAML 子集。"""
    lines = content.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, object] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line[:1].isspace():
            continue
        key, raw = line.split(":", 1)
        value = raw.strip().strip("\"'")
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError:
            if value.startswith("[") and value.endswith("]"):
                parsed = [
                    item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()
                ]
            else:
                parsed = value
        result[key.strip()] = parsed
    return result


def _normalize(path: str) -> str:
    value = path.strip().removeprefix("wiki/")
    if value and not value.endswith(".md"):
        value += ".md"
    return value


def read_page(root: Path, path: str) -> WikiPage:
    """读取单个 Wiki 页面；路径越界、非 .md 或不存在时抛 WikiPageNotFound。"""
    relative = _normalize(path)
    target = safe_resolve(root.resolve(), relative)
    if target is None or target.suffix.lower() != ".md" or not target.is_file():
        raise WikiPageNotFound(f"Wiki 页面不存在或不可访问: {path}")
    content = target.read_text(encoding="utf-8")
    return WikiPage(
        path=target.relative_to(root.resolve()).as_posix(),
        content=content,
        size=target.stat().st_size,
        updated_at=datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat(),
        metadata=_frontmatter(content),
    )


def read_source(source_records_root: Path, path: str) -> WikiPage:
    """通过专用只读边界读取单个来源记录。"""
    relative = _normalize(path.removeprefix("sources/"))
    source_root = source_records_root.resolve()
    target = safe_resolve(source_root, relative)
    if target is None or target.suffix.lower() != ".md" or not target.is_file():
        raise WikiPageNotFound(f"来源文件不存在或不可访问: {path}")
    content = target.read_text(encoding="utf-8")
    return WikiPage(
        path=f"sources/{target.relative_to(source_root).as_posix()}",
        content=content,
        size=target.stat().st_size,
        updated_at=datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat(),
        metadata=_frontmatter(content),
    )


def read_authorized_source(path: Path, *, label: str = "") -> WikiPage:
    """读取申请记录已授权的精确来源路径。

    路径不接受 HTTP 路由传入，调用方必须从服务端可信状态获取，
    如 issue 的私有上下文。
    """
    try:
        target = path.resolve(strict=True)
    except OSError as exc:
        raise WikiPageNotFound(f"来源文件不存在或不可访问: {label or path.name}") from exc
    if target.suffix.lower() != ".md" or not target.is_file():
        raise WikiPageNotFound(f"来源文件不存在或不可访问: {label or path.name}")
    content = target.read_text(encoding="utf-8")
    public_name = Path(label).name if label else target.name
    return WikiPage(
        path=f"sources/{public_name}",
        content=content,
        size=target.stat().st_size,
        updated_at=datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat(),
        metadata=_frontmatter(content),
    )


def search_pages(root: Path, query: str, *, limit: int = 30) -> list[WikiPage]:
    """按关键词在页面路径与正文中检索，返回至多 limit 个页面，读取失败的跳过。"""
    needle = query.strip().casefold()
    if not needle:
        return []
    pages: list[WikiPage] = []
    for path in sorted(root.resolve().rglob("*.md")):
        if not path.is_file():
            continue
        try:
            page = read_page(root, path.relative_to(root.resolve()).as_posix())
        except (OSError, UnicodeError, WikiPageNotFound):
            continue
        if needle in page.path.casefold() or needle in page.content.casefold():
            pages.append(page)
            if len(pages) >= limit:
                break
    return pages
