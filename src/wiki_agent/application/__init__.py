"""应用层：组装与用例边界。

只放应用服务（session.SessionService 会话用例、wiki_browser 只读查询）、
用例（issue_actions/compile_*…）与装配根。issue 读由适配器直用 issue_service。
执行引擎在 `wiki_agent.jobs`，快照账本与源 handler 在 `wiki_agent.sync`。

装配根（runtime）经 ``__getattr__`` 在首次访问时才导入：子模块被单独
import 时不应连带拉起 sync/agent/llm 整个执行栈——那会形成
consumer→application→runtime→consumer 的导入环，是否报错取决于导入顺序。
"""

from typing import TYPE_CHECKING

from wiki_agent.application.session import (
    InvalidInputError,
    MessageResult,
    ServiceError,
    SessionInfo,
    SessionMessage,
    SessionNotFoundError,
    SessionService,
)
from wiki_agent.application.wiki_browser import WikiBrowser, WikiFileInfo
from wiki_agent.events import AgentEvent, EventPublisher

if TYPE_CHECKING:
    from wiki_agent.application.runtime import AppRuntime

__all__ = [
    "AppRuntime",
    "AgentEvent",
    "EventPublisher",
    "InvalidInputError",
    "MessageResult",
    "SessionMessage",
    "ServiceError",
    "SessionInfo",
    "SessionNotFoundError",
    "WikiFileInfo",
    "SessionService",
    "WikiBrowser",
]

# 名字 → 所在模块；首次属性访问时导入并缓存进 globals
_LAZY = {
    "AppRuntime": "wiki_agent.application.runtime",
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
