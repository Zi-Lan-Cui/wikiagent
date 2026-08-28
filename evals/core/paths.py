"""统一评测路径解析和启动前检查。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_path(value: Path | str, *, kind: str, must_exist: bool = True) -> Path:
    """将 CLI 路径按项目根解析，并在启动阶段给出清晰错误。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if must_exist and not path.exists():
        raise FileNotFoundError(f"{kind} 不存在: {path}；请通过命令行参数提供有效路径")
    return path


def require_directory(value: Path | str, *, kind: str) -> Path:
    path = resolve_path(value, kind=kind)
    if not path.is_dir():
        raise NotADirectoryError(f"{kind} 必须是目录: {path}")
    return path


def require_file(value: Path | str, *, kind: str) -> Path:
    path = resolve_path(value, kind=kind)
    if not path.is_file():
        raise FileNotFoundError(f"{kind} 必须是文件: {path}")
    return path
