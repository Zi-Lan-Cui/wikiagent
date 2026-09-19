"""渲染层——终端渲染实体。

渲染器作为 hook 订阅事件驱动输出；hook 协议层不知道渲染层的存在。
"""

from wiki_agent.render.terminal_renderer import TerminalRenderer

__all__ = ["TerminalRenderer"]
