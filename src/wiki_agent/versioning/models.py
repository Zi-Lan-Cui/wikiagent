"""Wiki Git 运行记录模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GitRun:
    """一次 Wiki 写入运行的 Git 生命周期。"""

    run_id: str
    mode: str
    scope: str
    repo_root: Path
    scope_path: Path
    run_dir: Path
    before_commit: str
    after_commit: str = ""
    commit: str = ""
    status: str = "active"  # active/committed/aborted/rolled_back
    changed_files: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
