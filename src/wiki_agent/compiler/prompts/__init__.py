"""compiler prompt 模块组——模块即命名空间。

与状态变量耦合的 prompt（上下文压缩、agent 回合）留在对应模块旁，
不收进这里——拆开会断契约。
"""

from wiki_agent.compiler.prompts import compile as compile

__all__ = ["compile"]
