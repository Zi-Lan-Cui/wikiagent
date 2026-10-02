"""compiler prompt 模块组，模块直接作为命名空间使用。

与运行时状态耦合的 prompt（上下文压缩、agent 回合）留在原模块，
不集中到这里——它们随状态构造，拆出会切断与调用逻辑的联系。
"""

from wiki_agent.compiler.prompts import compile as compile

__all__ = ["compile"]
