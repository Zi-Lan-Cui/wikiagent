"""编译阶段观测钩子（机制层）。

管线只在阶段边界发出 CompileEvent，不预设消费者与存储形态：评测侧用它
落 run-evidence（阶段产物、index 快照、update 前像），其他策略层可各自
实现 CompileHook。默认无钩子；钩子异常不中断编译（记入日志后继续），
证据缺失优于编译陪葬，但必须留痕。

pre_execute 是钩子读旧页基线的唯一窗口：execute 会覆盖同一路径的页面。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from wiki_agent.log import get_logger

logger = get_logger("COMPILE_HOOKS")

CompileStage = Literal[
    "batch_start",  # 本批编译入口，携带当时 index.md 全文
    "extract",
    "search",
    "analyze",
    "plan",
    "pre_execute",  # execute 即将写页——update 前像只在这里还读得到
    "batch_end",  # 无论成功、无操作还是失败都发出，error 字段区分
]


@dataclass
class CompileEvent:
    """一次阶段边界事件。outcome 为管线正在构造的 IngestOutcome，
    各阶段模型随流水线推进逐步填充，钩子按 stage 读取所需字段。"""

    source: str
    stage: CompileStage
    wiki_dir: Path
    outcome: Any = None
    index_content: str | None = None
    error: Exception | None = None


@runtime_checkable
class CompileHook(Protocol):
    """阶段事件消费者。实现必须同步且只做轻量落盘。"""

    def on_compile_event(self, event: CompileEvent) -> None: ...


def emit_compile_event(hooks: Sequence[CompileHook], event: CompileEvent) -> None:
    """逐个派发；单个钩子失败只记日志，不影响编译与其他钩子。"""
    for hook in hooks:
        try:
            hook.on_compile_event(event)
        except Exception as exc:
            logger.warning(
                "编译钩子 %s 在 %s/%s 失败: %s",
                type(hook).__name__,
                event.source,
                event.stage,
                exc,
            )
