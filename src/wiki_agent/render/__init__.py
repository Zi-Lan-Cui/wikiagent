"""渲染层——终端渲染实体（独立于 hook 协议层）。

TerminalRenderer 直接继承 AgentHook 订阅事件——渲染层依赖
hook 协议（hook/base.py），hook 层不知道渲染的存在。
"""

from wiki_agent.render.terminal_renderer import TerminalRenderer

__all__ = ["TerminalRenderer"]
