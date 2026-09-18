"""结构重组的备份、快照与回滚（无 LLM）。

破坏性操作先备份；同 group_id 的 create+trim 是一个事务，失败整组回滚。
备份回滚逻辑只在 transaction 存在（蓝图约束）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from wiki_agent.compiler.restructure.models import Proposal
from wiki_agent.log import get_logger

logger = get_logger("RESTRUCTURE")


def _backup(
    wiki: Path, pages: dict[str, dict], slugs: set[str], backup_dir: Path | None
) -> list[str]:
    """执行前备份受影响文件 + index——破坏性操作可回滚。

    Args:
        wiki: wiki 根目录。
        pages: 页面表。
        slugs: 受影响页面集合。
        backup_dir: 备份目录（None 跳过备份）。

    Returns:
        备份的文件相对路径列表。
    """
    if backup_dir is None:
        return []
    backup_dir.mkdir(parents=True, exist_ok=True)
    backed: list[str] = []
    for slug in sorted(slugs):
        p = pages[slug]["path"]
        rel = p.relative_to(wiki)
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        backed.append(str(rel))
    index_path = wiki / "index.md"
    if index_path.exists():
        shutil.copy2(index_path, backup_dir / "index.md")
        backed.append("index.md")
    logger.info("  备份 %d 个文件 → %s", len(backed), backup_dir)
    return backed


def _snapshot_group(wiki: Path, proposals: list[Proposal]) -> dict[Path, bytes | None]:
    """保存一组操作涉及的文件；None 表示执行前不存在。"""
    paths = {wiki / "index.md"}
    for prop in proposals:
        paths.update(wiki / f"{slug}.md" for slug in prop.pages)
        if prop.target:
            paths.add(wiki / f"{prop.target}.md")
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _restore_group(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
