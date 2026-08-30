"""Wiki 知识库导航工具。

三个基础原语覆盖 wiki 探索的全部需求：
- ReadFile  — 读取页面全文
- ListDir   — 浏览目录结构
- Grep      — 全文搜索定位页面
"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.log import get_logger
from wiki_agent.tools.base import BaseTool

logger = get_logger("WIKI_TOOLS")

# Internal build artifacts and source provenance are implementation details,
# not part of the user-facing knowledge base.  Keep these out of all three
# navigation primitives so the model cannot accidentally treat logs/source
# snapshots as facts.
_HIDDEN_DIRS = frozenset({".logs", "sources"})


# ════════════════════════════════════════════════════════════
#  路径安全校验（三个工具共享）
# ════════════════════════════════════════════════════════════


def _safe_resolve(base: Path, rel: str, *, allow_root: bool = False) -> Path | None:
    """把相对路径安全解析到 base 根下。

    两层防护:
    1. 段检查（语义层）——拒绝绝对路径和任何 ``..`` 段；
       ``allow_root=True`` 时额外允许空串和 ``.``（表示根目录本身）。
    2. resolve + startswith（防御层）——兜底，绝对不可能出根。

    Args:
        base: 允许访问的根目录。
        rel: 相对路径。
        allow_root: 允许空串/``.``（表示根目录本身）。

    Returns:
        解析后的绝对路径；非法路径返回 None。
    """
    p = Path(rel)
    if p.is_absolute():
        return None
    parts = p.parts
    if allow_root and (not rel.strip() or rel.strip() == "."):
        return base
    if not parts or any(seg == ".." for seg in parts):
        return None
    if any(seg in _HIDDEN_DIRS for seg in parts):
        return None
    target = (base / p).resolve()
    if str(target).startswith(str(base)):
        return target
    return None


class ReadFile(BaseTool):
    """读取文件的全部内容。"""

    name: str = "ReadFile"
    description: str = (
        "读取 wiki 页面全文。也用于读取被转存的工具结果"
        "（转存提示中的 tmp/ 开头相对路径）。"
        "路径必须是相对路径，不含 .. 段。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "wiki 内相对路径（如 index.md、concepts/lambda.md），"
                    "或转存提示给出的 tmp/ 开头路径。"
                    "仅限相对路径，不能用 .. 或绝对路径"
                ),
            },
        },
        "required": ["file_path"],
    }

    def __init__(self, root: str | Path, workspace: str | Path | None = None):
        self._root = Path(root).resolve()
        # tmp/ 前缀的转存路径解析到 workspace 下，其余相对路径解析到 wiki 下
        self._workspace = Path(workspace).resolve() if workspace else None

    def _resolve(self, file_path: str) -> Path | None:
        """解析相对路径: tmp/ 开头 → workspace/，否则 → wiki root/。

        Args:
            file_path: 相对路径。

        Returns:
            解析后的绝对路径；非法路径返回 None。
        """
        if self._workspace and file_path.startswith("tmp/"):
            return _safe_resolve(self._workspace, file_path)
        return _safe_resolve(self._root, file_path)

    async def execute_once(self, file_path: str) -> str:
        target = self._resolve(file_path)
        if target is None:
            return self.error_result(
                "invalid_path",
                "路径必须是 wiki 内的相对路径，不能包含 .. 或绝对路径。",
                next_action="改用类似 concepts/lambda.md 的相对路径。",
            )
        try:
            return target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self.error_result(
                "not_found",
                f"文件不存在：{file_path}",
                next_action="先调用 ListDir 确认目录和文件名，再重新读取。",
            )
        except IsADirectoryError:
            return self.error_result(
                "not_a_file",
                f"{file_path} 是目录，不能按文件读取。",
                next_action="改用 ListDir 查看目录内容。",
            )
        except UnicodeDecodeError:
            return self.error_result(
                "unsupported_encoding",
                f"无法以 UTF-8 读取：{file_path}",
                next_action="改读 UTF-8 文本文件，或告知用户该文件无法作为 wiki 文本处理。",
            )


class ListDir(BaseTool):
    """列出目录内容，支持分页。"""

    name: str = "ListDir"
    description: str = (
        "列出 wiki 目录下的文件和子目录。每页最多 30 项，"
        "看到 '还有 N 项' 时用 offset 参数翻页继续看。"
        "路径仅限相对路径，不能用 .. 或绝对路径。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "dir_path": {
                "type": "string",
                "description": (
                    "wiki 内目录路径，如 concepts/、entities/；"
                    "空字符串或 . 表示根目录。不能用 .. 或绝对路径"
                ),
            },
            "offset": {
                "type": "integer",
                "description": "从第几项开始（默认 0）。看到'还有 N 项'时用 offset 翻页",
            },
        },
        "required": ["dir_path"],
    }

    _PAGE_SIZE = 30

    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()

    async def execute_once(self, dir_path: str, offset: int = 0) -> str:
        """列出目录内容（分页）。

        Args:
            dir_path: wiki 内目录路径（空串/``.`` 表示根目录）。
            offset: 起始项下标。

        Returns:
            格式化列表文本（含翻页提示）。
        """
        target = _safe_resolve(self._root, dir_path, allow_root=True)
        if target is None:
            return self.error_result(
                "invalid_path",
                "目录路径必须是 wiki 内的相对路径，不能包含 .. 或绝对路径。",
                next_action="改用类似 concepts/ 的相对目录路径。",
            )
        if not target.is_dir():
            return self.error_result(
                "not_a_directory",
                f"目录不存在：{dir_path or 'wiki/'}",
                next_action="检查目录名称，或从根目录调用 ListDir。",
            )
        if offset < 0:
            return self.error_result(
                "invalid_offset",
                f"offset 不能为负数：{offset}",
                next_action="使用不小于 0 的 offset。",
            )
        entries = sorted(
            (entry for entry in target.iterdir() if entry.name not in _HIDDEN_DIRS),
            key=lambda p: (p.is_file(), p.name),
        )

        if offset >= len(entries):
            if not entries:
                return self.error_result(
                    "empty_directory",
                    f"目录为空：{dir_path or 'wiki/'}",
                    next_action="不要继续翻页；改查其他目录或结束检索。",
                )
            return self.error_result(
                "invalid_offset",
                f"offset={offset} 超出目录项数 {len(entries)}。",
                next_action="把 offset 调整到目录范围内，或停止翻页。",
            )

        page = entries[offset : offset + self._PAGE_SIZE]
        has_more = offset + self._PAGE_SIZE < len(entries)

        lines = [f"# {dir_path} (共 {len(entries)} 项, 显示 {offset + 1}-{offset + len(page)})"]
        for p in page:
            suffix = "/" if p.is_dir() else f" ({_fmt_size(p)})"
            lines.append(f"  {p.name}{suffix}")
        if has_more:
            lines.append(
                f"  ... 还有 {len(entries) - offset - len(page)} 项，"
                f'继续看请调用 ListDir(dir_path="{dir_path}", offset={offset + len(page)})'
            )
        return "\n".join(lines)


class Grep(BaseTool):
    """在文件中搜索匹配文本。"""

    name: str = "Grep"
    description: str = (
        "在 wiki 中搜索包含指定文本的页面。返回匹配的文件路径和行片段。"
        "用于快速定位提到某个概念或术语的页面。"
        "路径仅限相对路径，不能用 .. 或绝对路径。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "要搜索的文本，支持正则表达式",
            },
            "in_dir": {
                "type": "string",
                "description": (
                    "限定搜索目录，如 concepts/、entities/。"
                    "空字符串表示搜索全部 wiki。不能用 .. 或绝对路径"
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少条匹配（默认 15）",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()

    async def execute_once(self, pattern: str, in_dir: str = "", max_results: int = 15) -> str:
        """在 wiki 中搜索匹配文本。

        Args:
            pattern: 搜索文本（支持正则，忽略大小写）。
            in_dir: 限定目录（空串 = 全 wiki）。
            max_results: 最多返回的匹配条数。

        Returns:
            格式化搜索结果（路径:行号:片段）。
        """
        import re as _re

        search_dir = _safe_resolve(self._root, in_dir, allow_root=True)
        if search_dir is None:
            return self.error_result(
                "invalid_path",
                "搜索目录必须是 wiki 内的相对路径，不能包含 .. 或绝对路径。",
                next_action="改用类似 concepts/ 的相对目录路径。",
            )
        if not search_dir.is_dir():
            return self.error_result(
                "not_a_directory",
                f"目录不存在：{in_dir or 'wiki/'}",
                next_action="检查目录名称，或省略 in_dir 搜索整个 wiki。",
            )

        if max_results <= 0:
            return self.error_result(
                "invalid_argument",
                f"max_results 必须大于 0：{max_results}",
                next_action="把 max_results 改成正整数。",
            )

        results: list[str] = []
        try:
            regex = _re.compile(pattern, _re.IGNORECASE)
        except _re.error as e:
            return self.error_result(
                "invalid_pattern",
                f"正则表达式无法解析：{e}",
                next_action="修正正则表达式；如果不需要正则，请使用简单文本模式。",
            )

        for md in sorted(search_dir.rglob("*.md")):
            if any(part in _HIDDEN_DIRS for part in md.relative_to(self._root).parts):
                continue
            if len(results) >= max_results * 3:
                break
            try:
                for lineno, line in enumerate(md.read_text(encoding="utf-8").split("\n"), 1):
                    if results and len(results) >= max_results * 3:
                        break
                    m = regex.search(line)
                    if m:
                        rel = str(md.relative_to(self._root))
                        snippet = line.strip()[:120]
                        results.append(f"{rel}:{lineno}: {snippet}")
            except Exception:
                continue

        if not results:
            return f"在 {in_dir or 'wiki/'} 中未找到匹配 '{pattern}' 的内容"

        deduped = list(dict.fromkeys(results))  # 保持顺序去重
        shown = deduped[:max_results]
        out = [f"# 搜索 '{pattern}' — 找到 {len(deduped)} 条匹配"]
        out.extend(shown)
        if len(deduped) > max_results:
            out.append(f"... 还有 {len(deduped) - max_results} 条")
        return "\n".join(out)


def _fmt_size(p: Path) -> str:
    size = p.stat().st_size
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"
