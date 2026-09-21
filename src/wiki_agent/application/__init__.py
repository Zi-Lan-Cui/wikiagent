"""Application composition and use-case boundaries.

装配根（runtime/job_worker）经 ``__getattr__`` 在首次访问时才导入：
子模块（job_results、compile_batches…）被单独 import 时不应连带拉起
sync/agent/llm 整个执行栈——那会形成 consumer→application→runtime→
consumer 的导入环，只在测试导入顺序凑巧时才不炸。
"""

from typing import TYPE_CHECKING

from wiki_agent.application.service import (
    InvalidInputError,
    MessageResult,
    ServiceError,
    SessionInfo,
    SessionMessage,
    SessionNotFoundError,
    WikiAgentService,
    WikiFileInfo,
)
from wiki_agent.events import AgentEvent, EventPublisher

if TYPE_CHECKING:
    from wiki_agent.application.job_worker import JobWorker
    from wiki_agent.application.runtime import AppRuntime

__all__ = [
    "AppRuntime",
    "AgentEvent",
    "EventPublisher",
    "JobWorker",
    "InvalidInputError",
    "MessageResult",
    "SessionMessage",
    "ServiceError",
    "SessionInfo",
    "SessionNotFoundError",
    "WikiFileInfo",
    "WikiAgentService",
]

# 名字 → 所在模块；首次属性访问时导入并缓存进 globals
_LAZY = {
    "AppRuntime": "wiki_agent.application.runtime",
    "JobWorker": "wiki_agent.application.job_worker",
}


def __getattr__(name: str):  # noqa: ANN201 - 动态转发，类型随名字而定
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
