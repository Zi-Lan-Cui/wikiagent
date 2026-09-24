"""wiki 只读查询：文件清单、页面与来源读取、路径检索，以及按问题定位资源页。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wiki_agent.issues import IssueStore
from wiki_agent.wiki import (
    WikiPage,
    read_authorized_source,
    read_page,
    read_source,
    search_pages,
)


@dataclass(frozen=True, slots=True)
class WikiFileInfo:
    """生成 wiki 文件的只读元数据。"""

    path: str
    size: int
    updated_at: str


class WikiBrowser:
    """wiki 浏览读模型。get_issue_resource 按 issue 记录定位其资源页，
    含授权边界判断（私有路径不外露），本质仍是 wiki 文件读取。"""

    def __init__(
        self,
        *,
        wiki_dir: Path,
        source_records_dir: Path,
        issue_store: IssueStore,
        project_root: Path,
    ) -> None:
        self._wiki_dir = wiki_dir
        self._source_records_dir = source_records_dir
        self._issues = issue_store
        self._project_root = project_root

    def list_wiki_files(self) -> list[WikiFileInfo]:
        """列出 wiki 根下的生成 Markdown 文件。"""
        if not self._wiki_dir.is_dir():
            return []
        files: list[WikiFileInfo] = []
        for path in sorted(self._wiki_dir.rglob("*.md")):
            if not path.is_file() or any(part.startswith(".") for part in path.parts):
                continue
            stat = path.stat()
            files.append(
                WikiFileInfo(
                    path=path.relative_to(self._wiki_dir).as_posix(),
                    size=stat.st_size,
                    updated_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                )
            )
        return files

    def get_wiki_page(self, path: str) -> WikiPage:
        """按安全解析规则读取单个公开 wiki 页。"""
        return read_page(self._wiki_dir, path)

    def get_wiki_source(self, path: str) -> WikiPage:
        """经 web 读边界读取单个来源档案页。"""
        return read_source(self._source_records_dir, path)

    def search_wiki_pages(self, query: str, *, limit: int = 30) -> list[WikiPage]:
        """检索公开 wiki 页的路径与内容。"""
        return search_pages(self._wiki_dir, query, limit=limit)

    def get_issue_resource(self, issue_id: str) -> WikiPage:
        """读取问题绑定的资源页，不暴露其私有路径。"""
        issue = self._issues.require(issue_id)
        public_path = str(issue.resource.get("path") or issue.resource.get("label") or "")
        source_path = issue.context.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            target = Path(source_path)
            if not target.is_absolute():
                target = self._project_root / target
            try:
                relative = target.resolve().relative_to(self._wiki_dir.resolve())
            except ValueError:
                return read_authorized_source(target, label=public_path)
            return read_page(self._wiki_dir, relative.as_posix())
        if issue.resource.get("type") == "wiki_page":
            return read_page(self._wiki_dir, public_path)
        return read_source(self._source_records_dir, public_path)
