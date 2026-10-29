"""评判输入的 prompt 拼装"""

from __future__ import annotations

PER_FILE_BUDGET = 12_000

InputBlocks = str | list[tuple[str, str]]


def render_blocks(blocks: InputBlocks, budget: int = PER_FILE_BUDGET) -> str:
    """按文件独立截断拼装；str 为向后兼容的整体文本路径。"""
    if isinstance(blocks, str):
        return blocks if len(blocks) <= budget else blocks[:budget] + "\n…（材料已截断）"
    parts: list[str] = []
    for path, content in blocks:
        if len(content) <= budget:
            body = content
        else:
            body = content[:budget] + f"\n…（本文件截断于 {budget} 字符，其后内容不可见）"
        parts.append(f"### {path}\n{body}")
    return "\n\n".join(parts)
