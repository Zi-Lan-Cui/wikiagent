"""memory.md 投影：由 fold 结果单向渲染——改 md 在语义上等于往事件流追加事件。"""

from __future__ import annotations

from wiki_agent.memory.items import KINDS, MemoryItem

_KIND_TITLES = {
    "preference": "偏好",
    "identity": "身份",
    "project": "项目",
    "knowledge": "知识",
    "task_state": "任务状态",
}


def render_memory_md(active_items: list[MemoryItem]) -> str:
    """按 kind 分组渲染，每条一行 `- 文本`；不含 id 与时间戳，保持注入与阅读干净。"""
    lines = ["# 用户画像"]
    for kind in KINDS:
        items = [i for i in active_items if i.kind == kind]
        if not items:
            continue
        lines.append(f"\n## {_KIND_TITLES[kind]}")
        lines.extend(f"- {i.text}" for i in items)
    return "\n".join(lines) + "\n"


def render_debug_md(items: list[MemoryItem]) -> str:
    """带 id/状态/时间的调试视图，不参与注入。"""
    lines = ["# 记忆条目（调试）"]
    for i in items:
        suffix = f"invalid since {i.invalid_at} ({i.invalid_reason}, by {i.invalidated_by})" if i.invalid_at else "active"
        lines.append(f"- [{i.id}] ({i.kind}/{i.persistence}, {suffix}) {i.text}")
    return "\n".join(lines) + "\n"
