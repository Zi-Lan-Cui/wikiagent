"""Wiki 导航与 Wiki 工具共用的路径安全原语。"""

from __future__ import annotations

from pathlib import Path

HIDDEN_DIRS = frozenset({".git", ".logs", "sources"})


def safe_resolve(base: Path, relative: str, *, allow_root: bool = False) -> Path | None:
    """在 base 下解析相对路径，阻止越界访问与内部目录暴露。"""
    path = Path(relative)
    if path.is_absolute():
        return None
    parts = path.parts
    if allow_root and (not relative.strip() or relative.strip() == "."):
        return base
    if not parts or any(part == ".." for part in parts):
        return None
    if any(part in HIDDEN_DIRS for part in parts):
        return None
    target = (base / path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target
